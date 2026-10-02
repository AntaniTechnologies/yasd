YASD (Yet Another Strata Dashboard) v1.0.0 is a real-time terminal UI for monitoring Strata (https://github.com/antani/Strata) instances.

Copyright 2026 Antani Technologies BV — MIT License

## Core function

Spins up an async background poller (single `asyncio` loop, no thread/lock) that polls a Strata server every 500 ms, fetching data from `/health`, `/metrics`, and `/v1/status` endpoints concurrently, then renders the results in a flicker-free full-screen terminal dashboard using the Rich (https://github.com/Textualize/rich) library at 10 FPS.

## Dashboard panels

- **Header** — connection status (ONLINE / OFFLINE / SLEEPING), server address (stripped of scheme), Strata engine version, and timestamp
- **Metrics (left)** — state badge (IDLE / READING / GENERATING / QUEUED / SLEEPING), context usage bar with colour-coded thresholds (green ≤ 69.9 %, orange 70–85 %, red > 85 %, blinks > 95 %), prompt/generation progress or last-request summary, phase, token counts (prompt total, generated, elapsed), queue status, draft acceptance, and model name
- **Performance (left)** — prompt speed and generation speed displayed side-by-side as `X.X tok/s`, with source sub-labels (`now` / `this run` / `last req`)
- **Session (left)** — request count with start time, tokens read vs reused (with reuse %), tokens written
- **Engine (left)** — KV quant, expert slots / cache MB, speculative decoding (MTP draft budget, lookup, pool workers)
- **Hardware (right)** — 10 one-line cards (Speed, GPU load, VRAM, GPU temp, Power, PCIe, CPU, Disk read, Experts VRAM, System RAM), each with value plus server-side history sparkline
- **Requests (right, flex)** — newest N rows of Strata's request ring (Time, Status, Prompt, Reused, Output, Tok/s, Hit); columns collapse responsively on narrow terminals
- **Footer** — activity log of the last 5 state transitions

## Key technical details

- State badges mirror `serve/web/app.js`: `live.state` is `idle` | `reading` | `generating` | `unloaded`, and `queued > 0` overrides to QUEUED; `loaded=false` (or `unloaded`) renders SLEEPING, not OFFLINE
- Strata concurrency is 1 behind a FIFO queue, so there is no per-slot structure — one flat `StrataSnapshot` dataclass per poll cycle
- Speeds are read from the server, never re-derived: `live.tok_s` / `tok_s_mean` / `prefill_tok_s_mean`, with last-request `decode_tok_s` and `prompt_ms`-derived prefill as idle fallbacks (same as the web Monitor tab)
- Context usage is `prompt_tokens + generated` while busy, falling back to the last finished request while idle so the gauge never drops to 0 between requests; display is capped at 99.5 % so it never reads exactly 100 %
- `/health` is the only route that skips authorization, so it doubles as the connectivity probe and answers even when an API key is set; `--api-key` sends `Bearer` to every endpoint except `/health`
- Offline policy: 4 consecutive failed poll cycles (~2 s at 500 ms) before reporting OFFLINE; 2 consecutive successes to come back ONLINE; a 401 on `/metrics` while `/health` answers stays ONLINE and reports "API key rejected"; malformed JSON keeps the last good snapshot and never flips connectivity
- Number formatting mirrors `serve/web/app.js` (`_num` / `_kfmt` / `_gb`); sparklines bucket the server's 60 one-second history samples to terminal width, plotting each bucket at its hottest sample
- Layout is rebuilt every frame from terminal size: left column ratio 3 (Metrics flex + Performance/Session/Engine fixed 5 rows), right column ratio 4 (Hardware fixed 12 rows + Requests flex); compact mode drops Session/Engine when Metrics cannot keep its 12 rows; Metrics blank spacers are the first rows dropped on short terminals; labels abbreviate below 40 cols inner width (tiny mode below 30)
- Request rows fit the terminal after fixed-panel overhead, capped by `--requests N | all` (default 5; `all` asks the server for its whole ring via `?requests=all`)
- Accepts `--server` / `-s` flag for the Strata URL (default http://127.0.0.1:8080); auto-prepends `http://` if scheme is omitted
- Accepts `--api-key` for key-protected servers and `--requests N|all` for request-table depth
- Optional `--debug` flag writes raw `/health`, `/metrics`, and `/v1/status` responses as JSONL to the specified file
- Runs with Ctrl+C or SIGTERM graceful shutdown
- Runs with `screen=True` for full-screen terminal UI
