# CLAUDE.md — Project Context for YASD

## Project Overview

**YASD** (Yet Another Strata Dashboard) is a real-time terminal UI for monitoring `Strata` instances. It polls `/health`, `/metrics`, and `/v1/status` endpoints every 500 ms and renders a flicker-free dashboard at 10 FPS using the Rich library.

- **Version:** 1.1.0 (check `pyproject.toml` for current)
- **Language:** Python 3.10+
- **Single file:** `yasd.py` (~1450 lines)
- **License:** MIT (Copyright 2026 Antani Technologies BV)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    YASDApplication                      │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────┐  │
│  │  Collector   │   │    Layout    │   │   Live     │  │
│  │ (async loop) │──▶│  (Rich)      │──▶│  (10 FPS)  │  │
│  └──────────────┘   └──────────────┘   └────────────┘  │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│                   Strata server                         │
│  /health  /metrics  /v1/status  (all polled async)      │
└─────────────────────────────────────────────────────────┘
```

### Core Components

| Component | Purpose |
|-----------|---------|
| `StrataCollector` | Async background poller (500 ms interval). Fetches all 3 endpoints concurrently, parses JSON, builds snapshots, tracks connectivity, state log, and raw bodies for `--debug`. |
| `StrataSnapshot` | Immutable flat dataclass holding all metrics at one point in time (no per-slot structure — Strata concurrency is 1 behind a FIFO). |
| Panel builders (`make_*`) | Pure functions that build Rich `Panel` objects from a snapshot (`make_header`, `make_metrics_panel`, `make_performance_panel`, `make_session_panel`, `make_engine_panel`, `make_hardware_panel`, `make_requests_panel`, `make_footer`, `make_offline_panel`). |
| `YASDApplication` | Orchestrates collector, layout, Live render loop, signals, debug logging, compact-mode switching. |

## Dashboard Panels

1. **Header** — Connection status (ONLINE / OFFLINE / SLEEPING), server address (scheme stripped), Strata engine version, timestamp
2. **Metrics (left)** — State badge (IDLE / READING / GENERATING / QUEUED / SLEEPING), context usage bar (color-coded: green ≤69.9%, orange 70–85%, red >85%, blinks >95%), prompt/generation progress or last-request summary, phase, token counts, queue, draft acceptance, model name
3. **Performance (left)** — Prompt speed & generation speed side-by-side as `X.X tok/s`, with source sub-labels (`now` / `this run` / `last req`)
4. **Session (left)** — Request count + start time, tokens read vs reused (with reuse %), tokens written
5. **Engine (left)** — KV quant, expert slots / cache MB, speculative decoding (MTP draft budget, lookup, pool workers)
6. **Hardware (right)** — 10 one-line cards (Speed, GPU load, VRAM, GPU temp, Power, PCIe, CPU, Disk read, Experts VRAM, System RAM), each with value + a btop-style braille history graph in its own colour
7. **Requests (right, flex)** — Newest N rows of the request ring (Time, Status, Prompt, Reused, Output, Tok/s, Hit); columns collapse responsively
8. **Footer** — Activity log of last 5 state transitions

## Key Technical Details

### State Detection (from `/metrics` live + `/health`)
- `live.state` is `idle` | `reading` | `generating` | `unloaded` (mirrors `serve/web/app.js`)
- `queued > 0` overrides to **QUEUED**
- `loaded=false` (or `unloaded`) → **SLEEPING** (reachable, not offline)
- ⚠️ There is no `/slots` endpoint and no per-slot structure — do not look for one

### Connectivity Policy
- `/health` is the only route that skips authorization → the connectivity probe
- OFFLINE only after 4 consecutive failed poll cycles (~2 s); 2 consecutive successes to recover
- 401 on `/metrics` while `/health` answers → stays ONLINE, reports "API key rejected" (`ApiKeyRejected`, `_AUTH` sentinel)
- `/metrics` unreachable but `/health` fine → ONLINE with "Metrics endpoint unavailable" (`MetricsUnavailable`)
- Malformed JSON keeps the last good snapshot, never flips connectivity
- Repeated connection resets rebuild the `aiohttp` session (hardened connector: `limit_per_host=8`, `enable_cleanup_closed`, `ttl_dns_cache=300`)

### Speeds
- Read from the server, never re-derived: `live.tok_s` / `tok_s_mean` / `prefill_tok_s_mean`
- Idle fallbacks (same as web Monitor tab): last request `decode_tok_s` for decode; `prompt_ms`-derived prefill for prefill
- See `_display_speeds()` in `yasd.py`

### Context Usage
- `prompt_tokens + generated` while busy, else last finished request while idle (gauge never drops to 0 between requests)
- Display capped at 99.5 % so it never reads exactly 100 %

### Number Formatting
- Mirrors `serve/web/app.js`: `_num` (thousands separators, em-dash for missing), `_kfmt` (k/M suffixes), `_gb` (binary GiB)
- Hardware graphs are btop-style braille cells (2 samples per character, 4 dot rows per cell, filled bottom-up, unlit cell = space); `_braille_level` keeps btop's upward bias and C++-style rounding, with a floor of one row for any non-zero sample
- Each card's graph is coloured from `GRAPH_COLORS` (card order); Rich downsamples to the terminal palette when truecolor is unavailable
- Graphs bucket the server's 60 one-second history samples to the card's cell width, each bucket drawn at its hottest sample; a short ring is left-padded so the newest samples stay flush right

### Layout
- Left column ratio 3 (Metrics flex + Performance/Session/Engine fixed 5 rows), right column ratio 4 (Hardware fixed 12 rows + Requests flex)
- Compact mode drops Session/Engine when Metrics cannot keep its 12 content rows; Metrics blank spacers go first on short terminals
- Labels abbreviate below 40 cols inner width, tiny mode below 30; short title below 96 cols total
- Request rows fit the terminal after fixed-panel overhead (`REQUEST_OVERHEAD`), capped by `--requests N | all` (`?requests=all` asks the server for its whole ring)

## CLI Interface

```bash
yasd [--server URL] [--api-key KEY] [--requests N|all] [--debug FILE]
```

- `-s, --server` — Strata URL (default: `http://127.0.0.1:8080`); auto-prepends `http://` if scheme omitted
- `--api-key` — Bearer key sent to every endpoint except `/health`
- `--requests` — Request rows to show: a count or `all` (default: `5`)
- `--debug` — Path to JSONL debug log; writes raw `/health`, `/metrics`, and `/v1/status` bodies each frame

## Dependencies

- `rich` — Terminal rendering
- `aiohttp` — Async HTTP client
- Python ≥ 3.10

## Development Notes

### Running
```bash
pip install -e .
yasd  # or: python yasd.py
```

### Key Files
- `yasd.py` — Single-file application
- `pyproject.toml` — Package metadata, entry point (`yasd = yasd:main`)
- `README.md` — User documentation
- `LICENSE` — MIT license

### Debugging
- Use `--debug dump.jsonl` to capture raw server responses
- Collector maintains `_history` (60 snapshots ≈ 30s at 500ms poll)
- `_state_log` tracks last 10 state transitions (footer shows last 5)

### Signal Handling
Graceful shutdown on `Ctrl+C` (SIGINT) and SIGTERM.

## Testing Strata locally

```bash
# From the Strata checkout
python serve/server.py --port 8080
```

## Extending / Modifying

| To change... | Look in... |
|--------------|------------|
| Poll interval | `POLL_INTERVAL = 0.5` / `StrataCollector.__init__` (`poll_interval`) |
| History length | `_history_max = 60` |
| State log size | `_log_max = 10` |
| Offline thresholds | `OFFLINE_THRESHOLD = 4`, reconnect-after-2 in `_poll_loop` |
| Color thresholds | `make_metrics_panel` (context bar), draft-acceptance bands |
| Panel layout | `create_layout()`, panel builder functions, `LEFT_RATIO` / `RIGHT_RATIO` |
| Endpoints | `StrataCollector` class constants (`HEALTH_ENDPOINT`, `METRICS_ENDPOINT`, `STATUS_ENDPOINT`) |
| Request depth | `--requests` flag → `request_rows()` / `?requests=all` |
