# YASD — Quick Start

## TL;DR

```bash
# 1. Start Strata:
python serve/server.py --port 8080

# 2. Run YASD, pointing it at the server:
python yasd.py -s http://127.0.0.1:8080
```

---

## What YASD needs

YASD reads **one data source**: Strata's HTTP interface. No log file, no extra flags, no wrapper script.

| Endpoint | What YASD gets |
|---|---|
| `/health` | Connectivity probe + `loaded` flag (answers even when an API key is set) |
| `/metrics` | Engine facts, live request, session totals, request ring, hardware + history sparklines |
| `/v1/status` | Draft-acceptance counters, service name, uptime, serving concurrency |

Just point YASD at a reachable Strata URL and it renders.

---

## Full server command

```bash
python serve/server.py --port 8080
```

| Flag | Required for YASD? | Purpose |
|---|---|---|
| `--port` | Yes (connect to it) | API server port |
| `--api-key` (server side) | Only if you set one | If set, pass the same key to YASD via `--api-key` |

---

## YASD CLI options

```
usage: yasd.py [-h] [--server SERVER] [--api-key API_KEY]
               [--requests REQUESTS] [--debug DEBUG]

YASD - Yet Another Strata Dashboard.

options:
  --server, -s    Strata URL (default: http://127.0.0.1:8080)
  --api-key       Bearer key sent to every endpoint except /health
  --requests      Request rows to show: a count, or 'all' (default: 5)
  --debug         Path to a JSONL debug log file
```

Example with all options:

```bash
python yasd.py \
  --server http://127.0.0.1:8080 \
  --api-key "$STRATA_API_KEY" \
  --requests all \
  --debug /tmp/yasd-debug.jsonl
```

- `--requests 10` shows the 10 newest requests (or fewer, if the terminal is short).
- `--requests all` asks the server for its whole ring (up to 500 kept) and fills the terminal.
- `--debug` writes the raw `/health`, `/metrics`, and `/v1/status` bodies as JSONL, one record per frame.

> **Note:** The `scheme` is optional — `python yasd.py -s 192.168.1.10:8080` works; `http://` is prepended automatically.

---

## Verifying it works

After starting both, you should see:

1. **Header** — `YASD ● ONLINE` with the server URL and Strata version
2. **Metrics** — State, context bar, progress/phase, token counts, queue, draft acceptance, model
3. **Performance** — Prompt & generation speed with source indicators (`now` / `this run` / `last req`)
4. **Session** — Request count, tokens read vs reused, tokens written
5. **Engine** — KV quant, expert slots / cache, speculative-decoding stats
6. **Hardware** — Speed, GPU, VRAM, temp, power, PCIe, CPU, disk, experts, RAM with sparklines
7. **Requests** — Newest request rows with status and token counts
8. **Activity Log** — State transitions at the bottom

If you see instead:

- `● OFFLINE` — Strata is unreachable at the header address (check host/port, firewall). YASD waits ~2 s of consecutive failures before reporting this.
- `● SLEEPING` — the server answers but the model is unloaded. Load a model; no need to restart YASD.
- `(API key rejected (HTTP 401))` next to `● ONLINE` — the server is reachable but your `--api-key` is wrong or missing.
