"""
YASD - Yet Another Strata Dashboard (v1.1.1)

A real-time terminal UI for monitoring Strata instances.

Copyright 2026 Antani Technologies BV

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import asyncio
import json
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Endpoints and polling
# ---------------------------------------------------------------------------

# /metrics carries everything (engine, live, requests, totals, hardware,
# history). /health is the only Strata route that skips _authorized(), so it
# doubles as the connectivity probe: it answers even when an API key is set.
# /v1/status adds the draft-acceptance counters (last_timings.draft_n*).
POLL_INTERVAL = 0.5

# Fallback graph width in braille cells; the panels compute a real width from
# the terminal.
HISTORY_WIDTH = 24

# One hue per hardware card, in card order (Speed, GPU load, VRAM, GPU temp,
# Power, PCIe, CPU, Disk read, Experts VRAM, System RAM), so no two graph lines
# read as the same series. Warm hues carry the thermal/power cards, cool hues
# the capacity ones; Rich folds them down to the terminal's own palette when
# truecolor is not available.
GRAPH_COLORS = (
    "#4ec9b0",  # Speed
    "#5aa9e6",  # GPU load
    "#9d8df1",  # VRAM
    "#f2777a",  # GPU temp
    "#f5a25d",  # Power
    "#f7d154",  # PCIe
    "#a9e34b",  # CPU
    "#6fd86f",  # Disk read
    "#4fbfe0",  # Experts VRAM
    "#c79be0",  # System RAM
)

# Consecutive failed poll cycles required before reporting OFFLINE.
# At a 500 ms poll interval this means ~2 s of genuine unreachability.
OFFLINE_THRESHOLD = 4

# How many request rows /metrics returns without ?requests=all.
DEFAULT_REQ_ROWS = 12

# Returned by the /metrics fetcher instead of a body when the server answers
# 401. Distinct from None ("not fetched"), so the poll loop can tell a bad key
# apart from an outage.
_AUTH = object()


class ApiKeyRejected(Exception):
    """The server is reachable but rejected our Authorization header."""


class MetricsUnavailable(Exception):
    """The server is reachable but /metrics did not answer. Not an outage."""


# ---------------------------------------------------------------------------
# Data model — one flat snapshot. Strata's concurrency is 1 behind a FIFO, so
# there is no per-slot structure to carry.
# ---------------------------------------------------------------------------

@dataclass
class StrataSnapshot:
    """Immutable snapshot of Strata's /metrics + /health + /v1/status at one instant."""
    timestamp: float

    # connectivity
    loaded: bool = True

    # state — `state` and `queued` are the only fields never null
    state: str = "idle"          # idle | reading | generating | unloaded
    queued: int = 0
    phase: Optional[str] = None

    # running request (null while idle — "no request yet" is not "zero tokens")
    prompt_tokens:    Optional[int] = None
    prompt_read:      Optional[int] = None
    prompt_total:     Optional[int] = None
    generated:        Optional[int] = None
    max_tokens:       Optional[int] = None
    elapsed_s:        Optional[float] = None

    # speeds — computed by the server, never re-derived here
    tok_s:              Optional[float] = None
    tok_s_mean:         Optional[float] = None
    prefill_tok_s_mean: Optional[float] = None

    # context
    max_context: int = 0
    ctx_used: int = 0            # prompt+generated, with last-request fallback

    # hardware
    gpu_util:         Optional[float] = None
    gpu_mem_used:     Optional[int] = None
    gpu_mem_total:    Optional[int] = None
    gpu_temp:         Optional[int] = None
    gpu_power:        Optional[float] = None
    gpu_power_limit:  Optional[float] = None
    gpu_pcie_gen:     Optional[int] = None
    gpu_pcie_gen_max: Optional[int] = None
    gpu_pcie_width:   Optional[int] = None
    gpu_pcie_rx_mb:   Optional[float] = None
    gpu_pcie_tx_mb:   Optional[float] = None
    cpu:              Optional[float] = None
    ram_used:         Optional[int] = None
    ram_total:        Optional[int] = None
    disk_read_mb:     Optional[float] = None
    disk_write_mb:    Optional[float] = None
    gpus: list[dict] = None      # type: ignore[assignment]

    # engine facts
    model: str = ""
    engine_version: str = ""
    kv: str = ""
    kv_resident: int = 0
    expert_slots: int = 0
    expert_cache_mib: int = 0
    expert_slots_primary: int = 0
    expert_cache_primary_mib: int = 0
    spec: int = 0
    mtp_max: int = 0
    lookup: int = 0
    pool_workers: int = 0
    conversation_cache_slots: int = 0
    images: bool = False
    cvec: Optional[str] = None

    # static hardware identity
    gpu_name: Optional[str] = None
    gpu_count: int = 1
    cpu_name: Optional[str] = None
    cores: int = 0
    threads: int = 0
    psutil: bool = False

    # session totals, request ring, server-side history series
    totals: dict = None          # type: ignore[assignment]
    requests: list[dict] = None  # type: ignore[assignment]
    requests_kept: int = 0
    history: dict[str, list[float]] = None  # type: ignore[assignment]

    # draft acceptance, from /v1/status.last_timings
    draft_n: int = 0
    draft_n_accepted: int = 0
    draft_valid: bool = True

    # service facts, from /v1/status
    service: str = ""
    uptime_s: Optional[float] = None
    concurrency_serving: int = 1

    def __post_init__(self):
        if self.totals is None:
            self.totals = {}
        if self.requests is None:
            self.requests = []
        if self.history is None:
            self.history = {}
        if self.gpus is None:
            self.gpus = []


# ---------------------------------------------------------------------------
# Badge — Strata's own vocabulary plus SLEEPING for an unloaded model
# ---------------------------------------------------------------------------

def _badge(snapshot: StrataSnapshot) -> str:
    """YASD badge for a snapshot.

    live.state is idle | reading | generating | unloaded, and queued > 0
    overrides it, exactly as serve/web/app.js picks the lit badge.
    """
    if not snapshot.loaded:
        return "SLEEPING"
    if snapshot.queued > 0:
        return "QUEUED"
    return {
        "reading":    "READING",
        "generating": "GENERATING",
        "unloaded":   "SLEEPING",
    }.get(snapshot.state, "IDLE")


# ---------------------------------------------------------------------------
# Formatting helpers — mirror serve/web/app.js so the terminal reads the same
# numbers as the Monitor tab.
# ---------------------------------------------------------------------------

def _num(n, d: int = 0) -> str:
    """app.js fmt(): thousands separators, em-dash for missing."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    return f"{n:,.{d}f}"


def _kfmt(n) -> str:
    """app.js kfmt(), extended with millions so session totals stay short."""
    if n is None:
        return "—"
    n = float(n)
    if n < 1000:
        return f"{n:,.0f}"
    if n < 1_000_000:
        return f"{n / 1000:.{1 if n < 10000 else 0}f}k"
    return f"{n / 1_000_000:.1f}M"


def _gb(b, d: int = 1) -> str:
    """app.js gb(): binary gigabytes, as Windows shows them."""
    if b is None:
        return "—"
    return f"{float(b) / 1073741824:,.{d}f}"


# Braille cell (U+2800 + bits) as btop uses it: two virtual columns and four
# dot rows per character. Bits are per dot, filled from the bottom up, so a
# graph of `width` cells plots 2 * width samples and each sample gets four
# vertical steps instead of one block.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40),   # left  column, bottom -> top
                 (0x08, 0x10, 0x20, 0x80))   # right column, bottom -> top


def _braille_cell(left: int, right: int) -> str:
    """One cell with `left`/`right` dot rows lit from the bottom up.

    An unlit cell is a space, as in btop's own symbol table: a series of all
    zeros then renders as blank space instead of 21 empty braille glyphs, which
    fonts without a Braille Patterns face would draw as tofu boxes.
    """
    bits = 0
    for column, level in enumerate((left, right)):
        for row in range(level):
            bits |= _BRAILLE_DOTS[column][row]
    return chr(0x2800 + bits) if bits else " "


# All 25 fill combinations, indexed as btop does: left * 5 + right, where each
# level is 0-4 lit dot rows out of the cell's four.
_BRAILLE_UP = tuple(_braille_cell(left, right)
                    for left in range(5) for right in range(5))


def _braille_level(value: float, top: float) -> int:
    """How many of a cell's four dot rows one sample fills (btop's mapping).

    btop's slight upward bias and C++-style rounding are kept so the curves
    match what btop shows; on top of that a sample that is not zero always
    lights one row, the way the old block sparkline drew a ▁ tick, so a busy
    card never reads as "no data".
    """
    if value <= 0:
        return 0
    scaled = min(1.0, value / top) * 4.0 + 0.3
    return max(1, min(4, int(scaled + 0.5)))


def _braille_graph(values, fixed_max: Optional[float] = None,
                   width: int = HISTORY_WIDTH) -> str:
    """Terminal equivalent of app.js spark(): one-second samples -> braille.

    Braille cells carry two samples side by side, so a `width`-wide card plots
    2 * width of them and the curve keeps its shape instead of collapsing into
    one block per character. Samples still outnumber the cells on a narrow
    card, so the series is bucketed and each bucket drawn at its hottest
    sample, which keeps single-sample spikes visible.
    """
    vals = [0.0 if v is None else float(v) for v in (values or [])]
    if width < 1:
        return ""
    if not vals:
        return " " * width                 # no history yet: keep the column full
    top = float(fixed_max or 0.0) or (max(vals) or 1e-9)
    if top <= 0:
        top = 1e-9

    cols = width * 2                       # virtual samples per cell row
    if len(vals) >= cols:
        buckets = [[] for _ in range(cols)]
        for i, v in enumerate(vals):
            buckets[min(cols - 1, i * cols // len(vals))].append(v)
        series = [max(b) if b else 0.0 for b in buckets]
    else:
        # Partial ring (fresh server, short history): pad on the left so the
        # newest samples stay flush with the right edge, as btop does.
        series = [0.0] * (cols - len(vals)) + vals

    out = []
    for i in range(0, cols, 2):
        left = _braille_level(series[i], top)
        right = _braille_level(series[i + 1], top)
        out.append(_BRAILLE_UP[left * 5 + right])
    return "".join(out)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class StrataCollector:
    """Async background collector for Strata's HTTP interface.

    Replaces threading.Lock + polling Thread with a single asyncio loop.
    Because the event loop is single-threaded, all mutations to shared state
    are inherently serial — no lock is ever needed.
    """

    HEALTH_ENDPOINT   = "/health"
    METRICS_ENDPOINT  = "/metrics"
    STATUS_ENDPOINT   = "/v1/status"

    OFFLINE_THRESHOLD = OFFLINE_THRESHOLD

    def __init__(self, server_url: str = "http://127.0.0.1:8080",
                 poll_interval: float = POLL_INTERVAL,
                 api_key: Optional[str] = None,
                 req_rows: Optional[int] = None):
        self.server_url    = server_url
        self.poll_interval = poll_interval
        self.api_key       = api_key
        # ?requests=all asks the server for its whole ring (up to 500 kept)
        # instead of the DEFAULT_REQ_ROWS it sends by default.
        self._all_requests = req_rows is None or req_rows > DEFAULT_REQ_ROWS

        self._running = False
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._session: Optional[aiohttp.ClientSession] = None

        self._current: StrataSnapshot = StrataSnapshot(timestamp=time.time())
        self._history: list[StrataSnapshot] = []
        self._history_max = 60          # ~30 s of history at 500 ms poll

        self._connected      = False
        self._last_error: Optional[str] = None
        self._consecutive_failures: int = 0
        self._success_after_offline: int = 0
        # True while the model is unloaded — the server is reachable, not offline.
        self._server_sleeping = False
        # True while /metrics answered 401 for our key.
        self._key_rejected = False

        self._state_log: list[tuple[float, str]] = []
        self._log_max   = 10
        self._last_state: str = ""      # empty so the first IDLE is recorded

        # Raw responses, for --debug
        self._last_health: Optional[dict] = None
        self._last_metrics: Optional[dict] = None
        self._last_status: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._session = self._make_session()
        self._poll_task = asyncio.create_task(self._poll_loop())

    @staticmethod
    def _make_session() -> aiohttp.ClientSession:
        """Create a session with a hardened connector.

        * limit_per_host keeps connection churn bounded.
        * enable_cleanup_closed proactively discards half-closed sockets,
          which a BaseHTTPRequestHandler without keep-alive produces when it
          drops idle connections (a classic source of spurious resets).
        """
        connector = aiohttp.TCPConnector(
            limit_per_host=8,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        return aiohttp.ClientSession(connector=connector)

    async def _recreate_session(self) -> None:
        """Discard all pooled connections and start fresh.

        Used after repeated connection failures — stale/poisoned keep-alive
        sockets would otherwise keep failing even though the server is fine.
        """
        if self._session:
            await self._session.close()
        self._session = self._make_session()

    async def stop(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # aiohttp helpers
    # ------------------------------------------------------------------

    async def _fetch_json(
        self, session: aiohttp.ClientSession, endpoint: str,
        auth: bool = True, ignore_errors: bool = False,
    ) -> Optional[dict]:
        """Fetch JSON from *endpoint*. Returns None on error if *ignore_errors*.

        *auth* is False only for /health, the one route Strata serves without
        checking the API key — sending a bad key there would turn a key error
        into a false OFFLINE.
        """
        # Generous timeouts: Strata can legitimately take seconds to answer
        # while under heavy prefill. A short total timeout made healthy servers
        # look offline.
        timeout = aiohttp.ClientTimeout(total=6.0, connect=2.0, sock_read=4.0)
        headers = {"Authorization": f"Bearer {self.api_key}"} \
            if (auth and self.api_key) else None
        try:
            async with session.get(
                f"{self.server_url}{endpoint}", timeout=timeout, headers=headers,
            ) as resp:
                resp.raise_for_status()
                return json.loads(await resp.text())
        except Exception:
            if ignore_errors:
                return None
            raise

    # ------------------------------------------------------------------
    # Collection loop
    # ------------------------------------------------------------------

    def _register_failure(self, message: str) -> None:
        """Count a failed poll cycle; flip OFFLINE only after a sustained outage."""
        self._consecutive_failures += 1
        self._last_error = message
        if self._consecutive_failures >= self.OFFLINE_THRESHOLD:
            self._connected = False

    async def _poll_loop(self) -> None:
        """Async polling loop — runs until *stop()* cancels this task.

        Error-handling policy:
        * Transport errors (timeout / connection reset) count toward the
          OFFLINE threshold (OFFLINE_THRESHOLD consecutive failures).
        * /health.loaded == false means "unloaded" — reachable, so it never
          counts as offline and is shown as SLEEPING instead.
        * A 401 on /metrics while /health answers is a key problem, not an
          outage: connectivity stays ONLINE and the key error is reported.
        * Data errors (bad JSON) keep the last good snapshot and never flip
          connectivity — they are a parsing problem, not an outage.
        """
        while self._running:
            try:
                snapshot = await self._fetch_and_parse()
                self._update_state(snapshot)

                self._current = snapshot
                self._history.append(snapshot)
                if len(self._history) > self._history_max:
                    self._history.pop(0)
                self._consecutive_failures = 0
                self._server_sleeping = not snapshot.loaded
                self._key_rejected = False
                if not self._connected:
                    self._success_after_offline += 1
                    if self._success_after_offline >= 2:
                        self._connected = True
                        self._success_after_offline = 0
                        self._last_error = None
                else:
                    self._last_error = None

            except asyncio.TimeoutError:
                # NOTE: caught before ClientConnectionError because
                # aiohttp.ServerTimeoutError inherits from both.
                self._register_failure("Request timeout")

            except ApiKeyRejected:
                # Only raised when /health answered, so the server is provably
                # reachable: stay ONLINE and blame the key, not the connection.
                self._key_rejected = True
                self._connected = True
                self._consecutive_failures = 0
                self._last_error = "API key rejected (HTTP 401)"

            except MetricsUnavailable:
                # /health answered but /metrics did not: reachable, no data.
                self._connected = True
                self._consecutive_failures = 0
                self._last_error = "Metrics endpoint unavailable"

            except aiohttp.ClientResponseError as e:
                self._register_failure(f"HTTP {e.status}")

            except aiohttp.ClientConnectionError as e:
                self._register_failure(f"Connection error ({type(e).__name__})")
                # Repeated resets usually mean stale pooled sockets —
                # rebuild the session so the next cycle starts clean.
                if self._consecutive_failures >= 2:
                    await self._recreate_session()

            except (json.JSONDecodeError, ValueError) as e:
                # Malformed / truncated response body: not a connectivity
                # problem. Keep the last good snapshot and retry next cycle.
                self._last_error = f"Bad response from server ({e})"
                self._consecutive_failures = 0

            except Exception as e:
                self._register_failure(f"{type(e).__name__}: {e}")

            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Fetch + parse
    # ------------------------------------------------------------------

    async def _fetch_and_parse(self) -> StrataSnapshot:
        """Fan out /health, /metrics and /v1/status concurrently, then parse."""
        now = time.time()
        snapshot = StrataSnapshot(timestamp=now)

        session = self._session  # guaranteed non-None after start()

        # /health is the primary endpoint — Strata answers it even when an API
        # key is set, so its failure is the only honest signal of an outage.
        # /metrics carries the data (optional: a 401 is handled below), /v1/status
        # only adds draft acceptance.
        health, metrics, status = await asyncio.gather(
            self._fetch_health(session),
            self._fetch_metrics(session),
            self._fetch_status(session),
        )
        self._last_health = health

        if metrics is _AUTH:
            raise ApiKeyRejected("API key rejected (HTTP 401)")
        if metrics is None:
            raise MetricsUnavailable("Metrics endpoint unavailable")
        self._last_metrics = metrics

        engine = metrics.get("engine") or {}
        live   = metrics.get("live") or {}
        hw     = metrics.get("hardware") or {}
        static = metrics.get("hardware_static") or {}
        last   = (metrics.get("requests") or [None])[0]

        # --- connectivity: /health.loaded ----------------------------------
        snapshot.loaded = bool(health.get("loaded", True)) if health else True

        # --- state ----------------------------------------------------------
        # live.state in idle | reading | generating | unloaded; queued > 0
        # overrides it, as the web app does.
        snapshot.state = live.get("state") or "idle"
        snapshot.queued = live.get("queued") or 0
        snapshot.phase = live.get("phase")

        # --- running request (nulls stay nulls) ----------------------------
        snapshot.prompt_tokens = live.get("prompt_tokens")
        snapshot.prompt_read   = live.get("prompt_read")
        snapshot.prompt_total  = live.get("prompt_total")
        snapshot.generated     = live.get("generated")
        snapshot.max_tokens    = live.get("max_tokens")
        snapshot.elapsed_s     = live.get("elapsed_s")

        # --- speeds: read, never re-derived --------------------------------
        snapshot.tok_s              = live.get("tok_s")
        snapshot.tok_s_mean         = live.get("tok_s_mean")
        snapshot.prefill_tok_s_mean = live.get("prefill_tok_s_mean")

        # --- context --------------------------------------------------------
        snapshot.max_context = int(engine.get("max_context") or 0)
        # app.js: the running request while busy, else the last finished one, so
        # the gauge does not drop to 0 between requests.
        if snapshot.state != "idle":
            snapshot.ctx_used = (live.get("prompt_tokens") or 0) + (live.get("generated") or 0)
        elif last:
            snapshot.ctx_used = (last.get("prompt_tokens") or 0) + (last.get("output_tokens") or 0)

        # --- hardware --------------------------------------------------------
        for k in ("gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp", "gpu_power",
                  "gpu_power_limit", "gpu_pcie_gen", "gpu_pcie_gen_max", "gpu_pcie_width",
                  "gpu_pcie_rx_mb", "gpu_pcie_tx_mb", "cpu", "ram_used", "ram_total",
                  "disk_read_mb", "disk_write_mb"):
            setattr(snapshot, k, hw.get(k))
        snapshot.gpus = hw.get("gpus") or []

        # --- engine facts ----------------------------------------------------
        snapshot.model          = engine.get("model") or (health or {}).get("model") or ""
        snapshot.engine_version = engine.get("version") or engine.get("engine") or ""
        snapshot.kv             = engine.get("kv") or ""
        snapshot.kv_resident    = engine.get("kv_resident") or 0
        snapshot.expert_slots   = engine.get("expert_slots") or 0
        snapshot.expert_cache_mib      = engine.get("expert_cache_mib") or 0
        snapshot.expert_slots_primary  = engine.get("expert_slots_primary") or 0
        snapshot.expert_cache_primary_mib = engine.get("expert_cache_primary_mib") or 0
        snapshot.spec           = engine.get("spec") or 0
        snapshot.mtp_max        = engine.get("mtp_max") or 0
        snapshot.lookup         = engine.get("lookup") or 0
        snapshot.pool_workers   = engine.get("pool_workers") or 0
        snapshot.conversation_cache_slots = engine.get("conversation_cache_slots") or 0
        snapshot.images         = bool(engine.get("images"))
        snapshot.cvec           = engine.get("cvec")

        # --- static hardware --------------------------------------------------
        snapshot.gpu_name  = static.get("gpu_name")
        snapshot.gpu_count = static.get("gpu_count") or 1
        snapshot.cpu_name  = static.get("cpu_name")
        snapshot.cores     = static.get("cores") or 0
        snapshot.threads   = static.get("threads") or 0
        snapshot.psutil    = bool(static.get("psutil"))

        # --- session totals, request ring, history series ---------------------
        snapshot.totals         = metrics.get("totals") or {}
        snapshot.requests       = metrics.get("requests") or []
        snapshot.requests_kept  = metrics.get("requests_kept") or len(snapshot.requests)
        snapshot.history        = metrics.get("history") or {}

        # --- draft acceptance (/v1/status) ------------------------------------
        if status:
            self._last_status = status
            timings = status.get("last_timings") or {}
            snapshot.draft_n          = timings.get("draft_n") or 0
            snapshot.draft_n_accepted = timings.get("draft_n_accepted") or 0
            snapshot.draft_valid = snapshot.draft_n_accepted <= snapshot.draft_n
            snapshot.service = status.get("service") or ""
            snapshot.uptime_s = status.get("uptime_s")
            snapshot.concurrency_serving = (status.get("concurrency") or {}).get("serving") or 1

        return snapshot

    async def _fetch_health(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """Fetch /health. Raises on error — this is the connectivity probe."""
        # No Authorization: /health is the route Strata serves without a key.
        return await self._fetch_json(session, self.HEALTH_ENDPOINT, auth=False)

    async def _fetch_metrics(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """Fetch /metrics. Returns the _AUTH sentinel on 401, None on other errors."""
        endpoint = self.METRICS_ENDPOINT + ("?requests=all" if self._all_requests else "")
        try:
            return await self._fetch_json(session, endpoint)
        except aiohttp.ClientResponseError as e:
            if e.status == 401:
                return _AUTH  # type: ignore[return-value]
            return None
        except Exception:
            return None

    async def _fetch_status(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """Fetch /v1/status. Optional — it only adds draft acceptance."""
        try:
            return await self._fetch_json(session, self.STATUS_ENDPOINT)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # State log
    # ------------------------------------------------------------------

    def _update_state(self, snapshot: StrataSnapshot) -> None:
        current_state = _badge(snapshot)
        if current_state != self._last_state:
            self._state_log.append((snapshot.timestamp, current_state))
            if len(self._state_log) > self._log_max:
                self._state_log.pop(0)
            self._last_state = current_state

    # ------------------------------------------------------------------
    # Public accessors (no locks — single-threaded event loop)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> StrataSnapshot:
        return self._current

    def is_connected(self) -> bool:
        return self._connected

    def is_sleeping(self) -> bool:
        """True while Strata reports the model unloaded — reachable, not offline."""
        return self._server_sleeping

    def is_key_rejected(self) -> bool:
        """True while /metrics answers 401 for our key."""
        return self._key_rejected

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def get_state_log(self) -> list[tuple[float, str]]:
        return list(self._state_log)

    def get_raw_responses(self) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
        """Return the most recent raw /health, /metrics and /v1/status bodies."""
        return self._last_health, self._last_metrics, self._last_status


# ---------------------------------------------------------------------------
# Layout builder — rebuilt every frame so --requests all fills the terminal
# ---------------------------------------------------------------------------

# One line per hardware card, see make_hardware_panel.
HARDWARE_ROWS = 10
# header(3) + footer(3) + hardware panel(10 rows + 2 borders)
# + requests panel borders + its table header row.
REQUEST_OVERHEAD = 3 + 3 + (HARDWARE_ROWS + 2) + 2 + 1

# Body split: the right column carries the request table and the graphs,
# so it needs more width than the label/value stack on the left.
LEFT_RATIO, RIGHT_RATIO = 3, 4

# Metrics is the only flex panel on the left; the other three are title +
# 3 content rows + border. Metrics always draws METRIC_ROWS rows, and the
# METRIC_SPACERS blank separators are the first to be dropped on a short
# terminal (Windows' default console is 80x25).
PANEL_ROWS    = 5
METRIC_ROWS   = 12
METRIC_SPACERS = 5


def _col_widths(term_width: int) -> tuple[int, int]:
    """Inner widths of the left and right body columns, borders removed.

    Rich's row splitter distributes ``term_width - 1`` columns over the
    ratios (one column is left over), so mirror that here instead of
    guessing: the panels then never overflow their column and nothing is
    truncated with an ellipsis.
    """
    avail = max(4, term_width - 1)
    left = int(avail * LEFT_RATIO / (LEFT_RATIO + RIGHT_RATIO))
    right = avail - left
    return max(12, left - 2), max(12, right - 2)


def create_layout(compact: bool = False) -> Layout:
    """Build the frame once; Rich re-fits it to the console every frame.

    *compact* drops the Session and Engine panels. On a short terminal they
    would steal rows from Metrics, which is the panel carrying the state, so
    they are the first to go.
    """
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left",  ratio=LEFT_RATIO),
        Layout(name="right", ratio=RIGHT_RATIO),
    )
    left = [Layout(name="metrics"), Layout(name="performance", size=5)]
    if not compact:
        left += [Layout(name="session", size=5), Layout(name="engine", size=5)]
    layout["left"].split(*left)
    layout["right"].split(
        Layout(name="hardware", size=HARDWARE_ROWS + 2),
        # Flex: fills the rest of the column. The number of rows drawn inside
        # it is capped by request_rows(), so --requests all never clips.
        Layout(name="requests"),
    )
    return layout


def request_rows(term_height: int, cap: Optional[int] = None) -> int:
    """How many request rows fit in the terminal, capped by --requests N.

    --requests all (cap None) lets the panel grow to whatever the terminal
    has left after the fixed panels.
    """
    fit = max(1, term_height - REQUEST_OVERHEAD)
    return min(cap, fit) if cap is not None else fit


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

def make_header(
    connected: bool,
    server_url: str = "",
    error: Optional[str] = None,
    sleeping: bool = False,
    snapshot: Optional[StrataSnapshot] = None,
    term_width: int = 100,
) -> Panel:
    # 80 columns is Windows' default console: the full tagline plus a date
    # stamp does not fit there, so narrow terminals get the short one.
    narrow = term_width < 96
    now = datetime.now().strftime("%H:%M:%S" if narrow else "%Y-%m-%d %H:%M:%S")

    title = Text()
    title.append("YASD", style="bold cyan")
    if not narrow:
        title.append(" Yet Another Strata Dashboard", style="dim white")
    if snapshot and snapshot.engine_version:
        title.append(f" · Strata {snapshot.engine_version}", style="bold cyan")

    if sleeping:
        # The server answers, the model does not. Not offline.
        status = Text("● SLEEPING", style="bold dark_orange")
        if error:
            status.append(f" ({error})", style="dim")
    elif connected:
        status = Text("● ONLINE", style="bold green")
        if error:
            # Reachable but something is wrong (e.g. the key was rejected).
            status.append(f" ({error})", style="bold yellow")
    else:
        status = Text("● OFFLINE / DISCONNECTED", style="bold red")
        if error:
            status.append(f" ({error})", style="dim red")

    # Right side: server address (stripped of scheme), then status icon
    if server_url:
        addr = server_url.removeprefix("https://").removeprefix("http://")
        right = Text()
        right.append(f"{addr}  ", style="dim yellow")
        right.append(status)
    else:
        right = status

    header = Table.grid(expand=True)
    header.add_column(justify="left")
    header.add_column(justify="center", ratio=1)
    header.add_column(justify="right")
    header.add_row(title, Text(now, style="bold yellow"), right)

    return Panel(header, style="bold white on blue")


def make_metrics_panel(snapshot: StrataSnapshot, term_width: int,
                       _frame: int = 0, spacers: bool = True) -> Panel:
    """Left panel: state, context, progress, token counts, queue, draft acceptance.

    *spacers* is False on short terminals: the blank separator lines are the
    only rows that carry no information, so they are the first to go.
    """

    # --- State badge ---------------------------------------------------
    badge = _badge(snapshot)
    badge_style = {
        "SLEEPING":   "dim",
        "QUEUED":     "bold black on dark_orange",
        "READING":    "bold white on magenta",
        "GENERATING": "bold white on green",
    }.get(badge, "bold white on blue")
    state_text = Text(badge, style=badge_style)

    # An 80-column terminal leaves 31 characters inside the left panel: the
    # long labels ("Context usage:") would push every value onto the next
    # line, so narrow terminals get abbreviated labels too. One extra column
    # is reserved so the value column always starts after a space.
    inner   = _col_widths(term_width)[0]
    # The panel border is already excluded from inner, but its one-column
    # padding on each side is not, so the grid itself gets two columns less.
    grid_w  = inner - 2
    narrow  = inner < 40
    # A 60-column terminal leaves 23: even abbreviated values wrap, so the
    # least informative digits are dropped too.
    tiny    = inner < 30
    label_w = 12 if narrow else 15

    def _lab(wide: str, short: str) -> str:
        return short if narrow else wide

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=label_w)
    table.add_column(style="bold white")

    def _gap() -> None:
        if spacers:
            table.add_row("")

    table.add_row("State:", state_text)
    _gap()

    # --- Context usage bar -----------------------------------------------
    # Context occupancy = full prompt length + generated tokens, with the
    # last finished request used while idle (app.js ctx-card).
    max_ctx    = snapshot.max_context if snapshot.max_context > 0 else 1
    ctx_tokens = snapshot.ctx_used
    ctx_ratio  = min(ctx_tokens / max_ctx, 1.0)
    # Guard: never display exactly 100.0% — cap at 99.5% to leave a visual
    # safety buffer. This also covers the idle-cached-state edge case.
    if ctx_ratio >= 1.0:
        ctx_ratio = 0.995
    # The bar fills whatever is left of the label column, so it never wraps:
    # brackets (2) + space + "99.5%" (5) = 8 characters.
    bar_w = max(3 if tiny else (4 if narrow else 8), grid_w - label_w - 8)
    ctx_filled    = int(ctx_ratio * bar_w)
    ctx_pct       = round(ctx_ratio * 100, 1)
    # "46.2%" needs a column of its own on a 60-column terminal, so the
    # decimal goes there; the bar itself still carries the fraction.
    pct = f"{ctx_ratio:.0%}" if tiny else f"{ctx_ratio:.1%}"

    # Colour-coded bar: green <= 69.9%, orange 70–85%, red > 85%.
    if ctx_pct > 85:
        bar_style = 'red'
    elif ctx_pct >= 70:
        bar_style = 'dark_orange'
    else:
        bar_style = 'green'

    # Blink when > 95 % — toggle visibility every 10 frames ≈ 1 s at 10 FPS.
    _blink_off = (ctx_pct > 95) and (_frame % 20 < 10)
    if _blink_off:
        context_bar = "░" * bar_w
        context_row = f"[dim][{context_bar}] {pct}[/dim]"
    else:
        context_bar = f'[{bar_style}]{"█" * ctx_filled}{"░" * (bar_w - ctx_filled)}[/{bar_style}]'
        context_row = f"[{context_bar}] {pct}"

    table.add_row(_lab("Max context:", "Max ctx:"),   f"{snapshot.max_context:,}")
    table.add_row(_lab("Context usage:", "Context:"), context_row)
    _gap()

    # --- Progress --------------------------------------------------------
    # Prompt progress is prompt_read/prompt_total, generation progress
    # generated/max_tokens — both computed by the server already.
    if snapshot.state == "reading" and snapshot.prompt_total:
        if tiny:
            table.add_row("Prompt:", f"{_kfmt(snapshot.prompt_read)}/"
                                     f"{_kfmt(snapshot.prompt_total)}")
        else:
            table.add_row("Prompt:", f"{snapshot.prompt_read or 0:,}/{snapshot.prompt_total:,} tok")
    elif snapshot.state == "generating" and snapshot.max_tokens:
        if tiny:
            table.add_row("Gen:", f"{_kfmt(snapshot.generated)}/"
                                  f"{_kfmt(snapshot.max_tokens)}")
        else:
            table.add_row("Generated:", f"{snapshot.generated or 0:,}/{snapshot.max_tokens:,} tok")
    else:
        last = (snapshot.requests or [None])[0]
        if last:
            if tiny:
                # The speed already has its own card, so only the length stays.
                table.add_row("Last req:", f"{_kfmt(last.get('output_tokens'))} tok")
            elif narrow:
                table.add_row("Last req:", f"{_kfmt(last.get('output_tokens'))} tok "
                                           f"@{_num(last.get('decode_tok_s'), 1)}t/s")
            else:
                table.add_row("Last request:", f"{_kfmt(last.get('output_tokens'))} tok "
                                               f"@ {_num(last.get('decode_tok_s'), 1)} t/s")
        else:
            table.add_row(_lab("Last request:", "Last req:"), "—")
    phase = snapshot.phase or "—"
    if tiny:
        phase = {"reading the prompt": "reading", "tool call complete": "tool",
                 "answering": "answer", "thinking": "think"}.get(phase, phase)
    elif narrow:
        phase = {"reading the prompt": "reading prompt",
                 "tool call complete": "tool call"}.get(phase, phase)
    table.add_row("Phase:", phase)
    _gap()

    # --- Token counts ----------------------------------------------------
    table.add_row(_lab("Prompt total:", "Prompt tot:"), f"{_kfmt(snapshot.prompt_tokens)}")
    table.add_row("Generated:", f"{_kfmt(snapshot.generated)}")
    table.add_row("Elapsed:",   f"{snapshot.elapsed_s:.1f} s"
                      if snapshot.elapsed_s is not None else "—")
    _gap()

    # --- Concurrency -----------------------------------------------------
    if tiny:
        table.add_row("Queue:", f"{snapshot.queued}q · {snapshot.concurrency_serving}srv")
    elif narrow:
        # "serving" alone does not fit the 16-character value column.
        table.add_row("Queue:", f"{snapshot.queued} queued · {snapshot.concurrency_serving} srv")
    else:
        table.add_row("Queue:", f"{snapshot.queued} queued · "
                                f"{snapshot.concurrency_serving} serving")
    _gap()

    # --- Draft acceptance (speculation) ----------------------------------
    if not snapshot.draft_valid:
        table.add_row(_lab("Draft accept:", "Draft acc:"), "invalid")
    elif snapshot.draft_n > 0:
        _acc = snapshot.draft_n_accepted / snapshot.draft_n * 100
        # ponytail: color thresholds are a UI heuristic; tune if draft-acc
        # semantics warrant different bands.
        if _acc <= 49.9:
            _acc_style = 'red'
        elif _acc <= 59.9:
            _acc_style = 'dark_orange'
        else:
            _acc_style = 'green'
        if tiny:
            # "181/255 (71.0%)" needs 16 columns; the parentheses and the
            # decimal are what do not fit, so they go.
            table.add_row("Draft acc:",
                          f"[{_acc_style}]{snapshot.draft_n_accepted:,}/{snapshot.draft_n:,} "
                          f"{_acc:.0f}%[/{_acc_style}]")
        else:
            table.add_row(_lab("Draft accept:", "Draft acc:"),
                          f"[{_acc_style}]{snapshot.draft_n_accepted:,}/{snapshot.draft_n:,} "
                          f"({_acc:.1f}%)[/{_acc_style}]")
    else:
        table.add_row(_lab("Draft accept:", "Draft acc:"), "—")

    # --- Model identification -------------------------------------------
    if snapshot.model:
        if spacers:
            # Roomy terminal: the name gets its own line at full panel width.
            table.add_row("Model:", "")
            name = snapshot.model if len(snapshot.model) <= grid_w \
                   else snapshot.model[:grid_w - 1] + "…"
            model_table = Table(expand=True, show_header=False, show_footer=False, box=None)
            model_table.add_column(style="dark_orange")
            model_table.add_row(name)
            return Panel(Group(table, model_table),
                         title="[bold]Metrics[/bold]", border_style="cyan")
        # Short terminal: inline, so the panel does not need a 13th row.
        room = grid_w - label_w
        table.add_row("Model:", snapshot.model if len(snapshot.model) <= room
                      else snapshot.model[:room - 1] + "…")

    return Panel(table, title="[bold]Metrics[/bold]", border_style="cyan")


# ---------------------------------------------------------------------------
# Display speeds — read from the server, with the web Monitor's idle fallbacks
# ---------------------------------------------------------------------------

def _display_speeds(snapshot: StrataSnapshot) -> tuple[Optional[float], Optional[float], str, str]:
    """(decode, prefill, decode label, prefill label) as app.js shows them."""
    last = (snapshot.requests or [None])[0]

    if snapshot.state == "generating":
        decode, decode_sub = snapshot.tok_s, "now"
    elif last:
        decode, decode_sub = last.get("decode_tok_s"), "last req"
    else:
        decode, decode_sub = None, ""

    if snapshot.state != "idle":
        prefill, prefill_sub = snapshot.prefill_tok_s_mean, "this run"
    elif last and (last.get("prompt_ms") or 0) > 0:
        read = (last.get("prompt_tokens") or 0) - (last.get("reused") or 0)
        prefill = read / (last["prompt_ms"] / 1000) if read > 0 else None
        prefill_sub = "last req"
    else:
        prefill, prefill_sub = None, ""

    return decode, prefill, decode_sub, prefill_sub


def make_performance_panel(snapshot: StrataSnapshot,
                           term_width: int = 100) -> Panel:
    """Left panel: prompt & generation speeds side by side on one line."""

    decode, prefill, decode_sub, prefill_sub = _display_speeds(snapshot)

    def _speed_text(value: Optional[float], active_style: str, unit_style: str) -> Text:
        if value is None:
            return Text("— tok/s", style="dim")
        t = Text(f"{value:.1f}", style=active_style if value > 0 else "dim")
        t.append(" tok/s", style=unit_style if value > 0 else "dim")
        return t

    prefill_text = _speed_text(prefill, "bold magenta", "dim magenta")
    infer_text   = _speed_text(decode, "bold green", "dim green")

    # The right column has to hold "GENERATION SPEED:", so on narrow
    # terminals the labels give way and the sub-lines name themselves instead.
    narrow = _col_widths(term_width)[0] < 40
    label_w  = 9 if narrow else 22
    lab_l    = "PROMPT:" if narrow else "PROMPT SPEED:"
    lab_r    = "GENERATE:" if narrow else "GENERATION SPEED:"
    sub_l    = prefill_sub if narrow else (f"Prefill {prefill_sub}" if prefill_sub else "")
    sub_r    = decode_sub if narrow else (f"Decode {decode_sub}" if decode_sub else "")

    # 3-column grid: label | divider | value — mirrors Metrics panel width.
    perf_table = Table.grid(expand=True)
    perf_table.add_column(style="cyan", width=label_w)
    perf_table.add_column(style="dim", width=1)
    perf_table.add_column(style="bold white")

    perf_table.add_row(lab_l, " ", lab_r)
    perf_table.add_row(prefill_text, " ", infer_text)
    perf_table.add_row(sub_l, " ", sub_r)

    return Panel(perf_table, title="[bold]Performance[/bold]", border_style="green", padding=(0, 0))


def make_session_panel(snapshot: StrataSnapshot,
                       term_width: int = 100) -> Panel:
    """Session totals — Strata's cache reuse is a first-class number."""
    t = snapshot.totals or {}
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")

    if not t or not t.get("requests"):
        table.add_row("Reqs:", "no requests yet")
        table.add_row("Read:", "—")
        table.add_row("Written:", "—")
        return Panel(table, title="[bold]Session[/bold]", border_style="cyan")

    read = (t.get("prompt_tokens") or 0) - (t.get("reused") or 0)
    reused = t.get("reused") or 0
    since = datetime.fromtimestamp(t["since"]).strftime("%H:%M") if t.get("since") else ""
    rate = reused / t["prompt_tokens"] * 100 if t.get("prompt_tokens") else None

    table.add_row("Reqs:", f"{t['requests']:,} since {since}")
    if _col_widths(term_width)[0] < 40:
        # "reused" is the wordiest part of the line; "of" keeps the same pair.
        table.add_row("Read:", f"{_kfmt(read)} of {_kfmt(t.get('prompt_tokens'))}"
                      + (f" ({rate:.0f}%)" if rate is not None else ""))
    else:
        table.add_row("Read:", f"{_kfmt(read)} · reused {_kfmt(reused)}"
                      + (f" ({rate:.0f}%)" if rate is not None else ""))
    table.add_row("Written:", f"{_kfmt(t.get('output_tokens'))} tokens")

    return Panel(table, title="[bold]Session[/bold]", border_style="cyan")


def make_engine_panel(snapshot: StrataSnapshot,
                      term_width: int = 100) -> Panel:
    """Engine facts Strata exposes and llama.cpp never did."""
    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8)
    table.add_column(style="bold white")

    kv = {"int8": "8-bit", "q4_0": "4-bit", "fp16": "16-bit"}.get(snapshot.kv, snapshot.kv)
    table.add_row("KV:", kv or "—")
    table.add_row("Experts:", f"{snapshot.expert_slots} slots · {snapshot.expert_cache_mib} MB")
    drafts = max(0, (snapshot.mtp_max or snapshot.spec) - 1) if snapshot.spec else 0
    if _col_widths(term_width)[0] < 40:
        # The pool size is the least useful of the three on an 80-column box.
        table.add_row("Spec:", f"MTP ≤{drafts} · lookup {snapshot.lookup}")
    else:
        table.add_row("Spec:", f"MTP ≤{drafts} · lookup {snapshot.lookup} "
                               f"· pool {snapshot.pool_workers}")

    return Panel(table, title="[bold]Engine[/bold]", border_style="magenta")


def make_hardware_panel(snapshot: StrataSnapshot, term_width: int) -> Panel:
    """The eight Monitor cards plus Experts in VRAM and System RAM.

    One line each: label, value, and a braille graph of the server's own
    one-second history ring — the terminal equivalent of the web app's SVG,
    drawn the way btop draws its graphs: two samples per character, four dot
    rows per cell, and a distinct colour per card.
    """
    h = snapshot.history or {}
    decode, prefill, decode_sub, prefill_sub = _display_speeds(snapshot)

    # Right column is ratio RIGHT_RATIO; subtract its borders.
    inner = _col_widths(term_width)[1]
    # Below 40 characters (a 60-column terminal) the "of 8 GB" style details
    # no longer fit next to the value, so they are folded into it or dropped.
    narrow  = inner < 40
    label_w = 12 if narrow else 13
    # The value+detail column is fixed so cards never wrap; the graph
    # absorbs whatever width is left. Two separators are reserved so a value
    # that exactly fills its column still leaves a gap before the cells.
    value_w = 18
    graph_w = max(6, inner - label_w - 3 - value_w)
    value_w = inner - label_w - 3 - graph_w

    # Cards are appended in GRAPH_COLORS order, so each one takes the next hue
    # and the ten lines stay tellable apart instead of blending into one grey.
    colors = iter(GRAPH_COLORS)

    def _card(label: str, value: str, sub: str, series: Optional[str],
              fixed_max: Optional[float], style: str) -> tuple[Text, Text, Text]:
        graph = _braille_graph(h.get(series, []), fixed_max, graph_w) if series else " " * graph_w
        left = Text(label[:label_w], style="cyan")
        mid = Text(value, style=style)
        if sub:
            mid.append("  " + sub, style="dim")
        if len(mid) > value_w:          # keep one line per card
            mid = Text(str(mid)[:value_w - 1] + "…", style=style)
        return left, mid, Text(graph, style=next(colors))

    rows = []

    speed_sub = decode_sub if not narrow else ""
    rows.append(_card("Speed", f"{decode:.1f} t/s" if decode is not None else "—",
                      speed_sub, "tok_s", None, "bold green"))

    multi = len(snapshot.gpus) > 1
    gpu_sub = (" · ".join(f"G{g.get('index')} {g.get('util')}%" for g in snapshot.gpus)
               if multi and not narrow else "")
    rows.append(_card("GPU load", f"{_num(snapshot.gpu_util)} %", gpu_sub,
                      "gpu_util", 100.0, "bold white"))

    if narrow:
        # "of 8 GB" does not fit beside the value, so the total is folded in.
        vram_val = f"{_gb(snapshot.gpu_mem_used)}/{_gb(snapshot.gpu_mem_total, 0)} GB" \
                   if snapshot.gpu_mem_total else f"{_gb(snapshot.gpu_mem_used)} GB"
    else:
        vram_val = f"{_gb(snapshot.gpu_mem_used)} GB"
    vram_sub = f"of {_gb(snapshot.gpu_mem_total, 0)} GB" \
               if (snapshot.gpu_mem_total and not narrow) else ""
    rows.append(_card("VRAM", vram_val, vram_sub,
                      "gpu_mem_used", float(snapshot.gpu_mem_total or 0), "bold white"))

    rows.append(_card("GPU temp", f"{_num(snapshot.gpu_temp)} °C", "",
                      "gpu_temp", 90.0, "bold white"))

    if narrow:
        power_val = f"{_num(snapshot.gpu_power, 1)}/{_num(snapshot.gpu_power_limit)} W" \
                    if snapshot.gpu_power_limit else f"{_num(snapshot.gpu_power, 1)} W"
        power_sub = ""
    else:
        power_val = f"{_num(snapshot.gpu_power, 1)} W"
        power_sub = f"of {_num(snapshot.gpu_power_limit)} W" if snapshot.gpu_power_limit else ""
    rows.append(_card("Power", power_val, power_sub,
                      "gpu_power", snapshot.gpu_power_limit, "bold white"))

    gen = snapshot.gpu_pcie_gen_max or snapshot.gpu_pcie_gen
    if narrow:
        # The rate already has its graph; the link description is what
        # has to stay readable.
        pcie_val = (f"Gen{gen} x{snapshot.gpu_pcie_width}"
                    if gen and snapshot.gpu_pcie_width else
                    f"Gen{gen}" if gen else "—")
        pcie_sub = ""
    else:
        pcie_val = (f"Gen{gen}"
                    + (f" x{snapshot.gpu_pcie_width}" if snapshot.gpu_pcie_width else "")
                    if gen else "—")
        pcie_sub = (f"{_num(snapshot.gpu_pcie_rx_mb, 1)} MB/s"
                    if snapshot.gpu_pcie_rx_mb is not None else "")
    rows.append(_card("PCIe", pcie_val, pcie_sub, "gpu_pcie_rx_mb", None, "bold white"))

    cpu_sub = f"{snapshot.cores}c · {snapshot.threads}t" if (snapshot.threads and not narrow) else ""
    rows.append(_card("CPU", f"{_num(snapshot.cpu, 1)} %", cpu_sub, "cpu", 100.0, "bold white"))

    if snapshot.disk_read_mb is None:
        disk_val, disk_sub = "—", ("needs psutil" if not snapshot.psutil else "")
    else:
        big = snapshot.disk_read_mb >= 1000
        disk_val = _num(snapshot.disk_read_mb / 1024, 2) if big else \
                   _num(snapshot.disk_read_mb, 1 if snapshot.disk_read_mb < 10 else 0)
        disk_sub = ("GB/s" if big else "MB/s")
        if snapshot.disk_write_mb is not None and not narrow:
            disk_sub += f" · w {_num(snapshot.disk_write_mb, 1)}"
    rows.append(_card("Disk read", disk_val, disk_sub, "disk_read_mb", None, "bold white"))

    exp_val = (f"{snapshot.expert_slots} × {snapshot.expert_cache_mib} MB"
               if snapshot.expert_slots else "—")
    rows.append(_card("Experts VRAM", exp_val, "", None, None, "bold white"))

    if narrow:
        ram_val = f"{_gb(snapshot.ram_used)}/{_gb(snapshot.ram_total, 0)} GB" \
                  if snapshot.ram_total else f"{_gb(snapshot.ram_used)} GB"
        ram_sub = ""
    else:
        ram_val = f"{_gb(snapshot.ram_used)} GB"
        ram_sub = f"of {_gb(snapshot.ram_total, 0)} GB" if snapshot.ram_total else ""
    rows.append(_card("System RAM", ram_val, ram_sub,
                      "ram_used", float(snapshot.ram_total or 0), "bold white"))

    # Exactly HARDWARE_ROWS lines: the eight Monitor cards plus Experts and RAM.
    rows = rows[:HARDWARE_ROWS]

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=13, justify="left")
    table.add_column(style="", width=inner - 13 - graph_w - 2, justify="left")
    # No column style: the graph cells bring their own per-card colour.
    table.add_column(style="", width=graph_w, justify="left")
    for left, mid, graph in rows:
        table.add_row(left, mid, graph)

    return Panel(table, title="[bold]Hardware[/bold]", border_style="yellow")


def make_requests_panel(snapshot: StrataSnapshot, term_width: int, rows: int) -> Panel:
    """Recent requests — the newest N of Strata's ring (newest first)."""
    requests = snapshot.requests or []
    kept = snapshot.requests_kept or len(requests)

    inner = _col_widths(term_width)[1]
    # Narrow boxes lose the columns whose numbers duplicate others: Reused is
    # Hit's complement, and Hit needs a percent sign. Time 8 + status 7 plus
    # one separator per numeric column must still leave room for the digits,
    # and 7 is also the narrowest width that keeps "Prompt" off its neighbour.
    cols = [("Prompt", "prompt_tokens"), ("Output", "output_tokens"),
            ("Tok/s", "decode_tok_s")]
    if inner >= 47:
        cols += [("Hit", "hit_rate")]
    if inner >= 55:
        cols.insert(1, ("Reused", "reused"))
    num_w = max(4, (inner - 8 - 7 - len(cols)) // len(cols))

    table = Table.grid(expand=True)
    table.add_column(style="cyan", width=8, justify="left")
    table.add_column(style="bold white", width=7, justify="left")
    for _ in cols:
        table.add_column(width=num_w, justify="right")

    badge = {"stop": "Done", "length": "Max tok", "cancel": "Stop",
             "disconnect": "Closed", "error": "Error"}

    table.add_row("Time", "Status", *(name for name, _ in cols))
    for r in requests[:rows]:
        t = datetime.fromtimestamp(r["time"]).strftime("%H:%M:%S") if r.get("time") else "—"
        cells = []
        for _, key in cols:
            if key == "hit_rate":
                cells.append(f"{r[key] * 100:.1f}%" if r.get(key) is not None else "—")
            elif key == "decode_tok_s":
                cells.append(_num(r.get(key), 1))
            else:
                cells.append(_kfmt(r.get(key)))
        table.add_row(t, badge.get(r.get("finish"), r.get("finish") or "—"), *cells)

    # The offline path swaps in make_offline_panel instead, so reaching this
    # function always means the last poll answered.
    title = f"[bold]Requests[/bold] [dim]· {min(rows, kept)} of {kept} kept[/dim]"
    return Panel(table, title=title, border_style="yellow")


def make_footer(collector: StrataCollector) -> Panel:
    """Activity log: last N state transitions."""
    state_log = collector.get_state_log()

    if not state_log:
        log_text = Text("Waiting for state transitions…", style="dim")
    else:
        log_text = Text()
        for i, (ts, state) in enumerate(state_log[-5:]):
            if i > 0:
                log_text.append(" → ", style="dim")
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            style = {"SLEEPING": "dim", "QUEUED": "dark_orange",
                     "READING": "magenta", "IDLE": "blue"}.get(state, "green")
            log_text.append(f"{time_str} {state}", style=style)

    return Panel(log_text, title="[bold]Activity Log[/bold]", border_style="dim")


def make_offline_panel(error: str) -> Panel:
    text = Text()
    text.append("⚠  SERVER OFFLINE\n\n",         style="bold red")
    text.append(f"Error: {error}\n\n",            style="yellow")
    text.append("Ensure Strata is running and reachable at the address in the header:\n", style="dim")
    text.append("  python serve/server.py --port 8080\n", style="cyan")
    text.append("If the server has an API key, pass it with --api-key.\n", style="dim")
    return Panel(text, title="[bold red]CONNECTION LOST[/bold red]", border_style="red")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class YASDApplication:
    def __init__(self, server_url: str = "http://127.0.0.1:8080",
                 debug_file: Optional[str] = None,
                 api_key: Optional[str] = None,
                 req_rows: Optional[int] = None):
        # Auto-prepend http:// if the user omitted the scheme
        if not server_url.startswith(("http://", "https://")):
            server_url = "http://" + server_url
        self.req_rows    = req_rows
        self.collector = StrataCollector(server_url=server_url, poll_interval=POLL_INTERVAL,
                                         api_key=api_key, req_rows=req_rows)
        self.console   = Console()
        self._compact  = False
        self.layout    = create_layout(self._compact)
        self._running  = True
        self._frame    = 0

        # Debug logging path (file handle opened lazily in run(), closed in finally)
        self._debug_path = debug_file
        self._debug_fp: Optional[any] = None  # type: ignore[assignment]

        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _log_debug(self) -> None:
        """Append a single JSONL record with the raw server responses."""
        if self._debug_fp is None:
            return
        health, metrics, status = self.collector.get_raw_responses()
        record = {"ts": datetime.now().isoformat(), "health": health}
        if metrics is not None and metrics is not _AUTH:
            record["metrics"] = metrics
        if status is not None:
            record["status"] = status
        self._debug_fp.write(json.dumps(record, default=str) + "\n")

    def _handle_signal(self, signum, frame) -> None:
        self._running = False

    def _update_layout(self) -> None:
        connected = self.collector.is_connected()
        sleeping  = self.collector.is_sleeping()
        error     = self.collector.get_last_error()
        snapshot  = self.collector.get_snapshot()
        height    = self.console.size.height or 24
        width     = self.console.size.width or 80
        self._frame += 1

        # Debug: write raw server responses to JSONL file
        if self._debug_fp is not None:
            self._log_debug()

        # Session and Engine need 5 rows each. When the left column cannot
        # give Metrics its 12 content rows on top of them, drop the two
        # panels — once, so the frame is only rebuilt on a resize.
        body    = height - 6
        compact = body < METRIC_ROWS + 3 * PANEL_ROWS
        if compact != self._compact:
            self._compact = compact
            self.layout = create_layout(compact)
        rows = request_rows(height, self.req_rows)

        self.layout["header"].update(
            make_header(connected, self.collector.server_url, error, sleeping,
                        snapshot, width)
        )

        # While SLEEPING the server is reachable — keep rendering the last
        # good snapshot instead of the offline panel.
        if connected or sleeping:
            # Whatever the left column has left for Metrics decides whether
            # its blank separator lines fit.
            spacers = (body - (PANEL_ROWS if compact else 3 * PANEL_ROWS)
                       ) >= METRIC_ROWS + METRIC_SPACERS
            self.layout["metrics"].update(
                make_metrics_panel(snapshot, width, self._frame, spacers))
            self.layout["performance"].update(make_performance_panel(snapshot, width))
            if not compact:
                self.layout["session"].update(make_session_panel(snapshot, width))
                self.layout["engine"].update(make_engine_panel(snapshot, width))
            self.layout["hardware"].update(make_hardware_panel(snapshot, width))
            self.layout["requests"].update(make_requests_panel(snapshot, width, rows))
        else:
            self.layout["body"].update(make_offline_panel(error or "Unknown error"))

        self.layout["footer"].update(make_footer(self.collector))

    async def run(self) -> None:
        # Open debug file lazily here so the handle is scoped to run()'s
        # lifecycle — even if run() is never called the file is never
        # opened, and if it is closed abnormally the finally below
        # guarantees cleanup.
        self._debug_fp: Optional[any] = None  # type: ignore[assignment]
        if self._debug_path:
            self._debug_fp = open(self._debug_path, "w", buffering=1)  # line-buffered
            print(f"[YASD] Debug log: {self._debug_path}")

        await self.collector.start()
        try:
            with Live(
                self.layout,
                console=self.console,
                refresh_per_second=10,
                screen=True,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                while self._running:
                    self._update_layout()
                    live.update(self.layout)
                    await asyncio.sleep(0.01)  # yields to event loop → collector runs
        finally:
            await self.collector.stop()
            if self._debug_fp is not None:
                self._debug_fp.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="YASD - Yet Another Strata Dashboard. © 2026 Antani Technologies BV.")
    parser.add_argument(
        "--server", "-s",
        default="http://127.0.0.1:8080",
        help="Strata URL (default: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--api-key",
        help="Bearer key sent to every endpoint except /health",
    )
    parser.add_argument(
        "--debug",
        help="Path to a JSONL debug log file; writes raw /health, /metrics and /v1/status responses each frame",
    )
    parser.add_argument(
        "--requests",
        default="12",
        help="Request rows to show: a count, or 'all' for every row the server keeps (default: 12)",
    )
    args = parser.parse_args()

    if args.requests.lower() == "all":
        req_rows: Optional[int] = None
    else:
        try:
            req_rows = max(1, int(args.requests))
        except ValueError:
            parser.error("--requests must be a count or 'all'")

    app = YASDApplication(server_url=args.server, debug_file=args.debug,
                          api_key=args.api_key, req_rows=req_rows)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()