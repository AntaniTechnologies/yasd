# YASD - Yet Another Strata Dashboard

<img width="1109" height="700" alt="yasd" src="https://github.com/user-attachments/assets/dc0ec4ff-54d5-43dc-bc61-74063640c336" />

A real-time terminal UI for monitoring Strata instances.
One file. One command. One screen. Zero fuss.

## Prerequisites

- Python 3.10+
- A running Strata instance

## Installation

```bash
pip install rich aiohttp
python yasd.py
```

Or as an installed command:

```bash
pip install -e .
yasd
```

## Usage

```bash
python yasd.py                                # connect to localhost:8080
python yasd.py --server 192.168.1.10:8080     # connect to a remote server (scheme optional)
python yasd.py --api-key "$STRATA_API_KEY"    # servers with an API key set
python yasd.py --requests all                 # fill the terminal with the whole request ring
python yasd.py --debug dump.jsonl             # log raw server responses as JSONL
```

## What it shows

The dashboard is split into a few panels:

- **Header** -- connection status (ONLINE / OFFLINE / SLEEPING), server address, Strata version, current time
- **Metrics** -- state (IDLE / READING / GENERATING / QUEUED / SLEEPING), context usage bar, prompt/generation progress, phase, token counts, queue, draft acceptance, model name
- **Performance** -- prompt speed and generation speed side by side
- **Session** -- request count, tokens read vs reused, tokens written
- **Engine** -- KV quant, expert slots / cache, speculative-decoding stats
- **Hardware** -- speed, GPU load, VRAM, temp, power, PCIe, CPU, disk, experts VRAM, system RAM, each with its own colour-coded braille history graph
- **Requests** -- newest rows of the request ring with status and token counts
- **Footer** -- last few state transitions in an activity log

While the model is unloaded the header reads SLEEPING (the server is reachable, so this is not an outage). If the server goes away the body is replaced by a CONNECTION LOST panel until it returns.

## Dependencies

- [rich](https://github.com/Textualize/rich) -- terminal rendering
- [aiohttp](https://docs.aiohttp.org/) -- async HTTP client

## License

MIT License. See [LICENSE](LICENSE) for the full text.

YASD is copyright (C) 2026 Antani Technologies BV.
