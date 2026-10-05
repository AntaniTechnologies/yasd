# Changelog

All notable changes to YASD (Yet Another Strata Dashboard) are documented here.

## [1.1.0] - 2026-10-04

### Changed
- Hardware panel: replaced the single-colour dim-grey ASCII block sparkline
  (`_sparkline`, `▁▂▃▄▅▆▇█`, one sample per character) with a btop-style
  braille history graph (`_braille_graph`, `yasd.py`).
- Graph cells carry two samples side by side with four dot rows per cell,
  filled bottom-up (`_braille_cell`, `_BRAILLE_UP` table indexed `left * 5 +
  right`, unlit cell renders as space so an all-zero series stays blank).
- Sample-to-row mapping (`_braille_level`) keeps btop's upward bias and
  C++-style rounding, with a floor of one lit row for any non-zero sample.
- Resolution doubled: a `width`-wide card now plots `2 * width` samples
  instead of one block per character.
- Bucketing unchanged in spirit: the server's 60 one-second history samples
  are bucketed to the card's cell width with each bucket drawn at its hottest
  sample; a short/partial ring is left-padded so the newest samples stay flush
  right.
- Each of the 10 hardware cards (Speed, GPU load, VRAM, GPU temp, Power,
  PCIe, CPU, Disk read, Experts VRAM, System RAM) now draws its graph in its
  own hue from the new `GRAPH_COLORS` tuple; the graph column lost its `dim`
  style so the colour survives (Rich folds it down when truecolor is
  unavailable).
- Edge handling: `width < 1` returns `""`, empty history returns spaces to
  keep the column full.
- Docs: `AGENTS.md`, `CLAUDE.md`, `QUICK-START.md`, `README.md` updated from
  "sparkline" wording to "btop-style braille history graph in its own colour"
  / "colour-coded braille graphs" / "history series".
- `README.md`: added a `Version 1.1.0` banner and simplified Prerequisites to
  "A running Strata instance" (dropped the `python serve/server.py --port
  8080` example).
- Version bumped to `1.1.0` in `pyproject.toml` and the `yasd.py` docstring.

### Fixed
- Adjacent hardware history lines no longer blend into one indistinguishable
  grey series; small-but-live series no longer read as missing data.
- All-zero history no longer renders as tofu boxes on fonts without a Braille
  Patterns face (renders blank instead).

### Verified
- Rendering checked at 120x40, 100x30, and 80x25; layout (ten one-line
  cards) untouched.

## [1.0.0] - 2026-10-02

- Initial release: real-time terminal UI polling Strata `/health`, `/metrics`,
  and `/v1/status` every 500 ms and rendering at 10 FPS with Rich.
