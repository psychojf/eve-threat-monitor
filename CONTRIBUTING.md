# Contributing to EVE Threat Monitor

Thanks for your interest! This is a hobby project / experiment, but
contributions are welcome. A few ground rules keep it healthy.

## The one hard rule: no automation

This tool is deliberately **passive**: it reads pixels, plays a sound, and
shows information. It never clicks, types, moves the mouse, or acts in-game
in any way.

**Pull requests that add any form of game automation or input injection
(auto-warp, auto-dock, keystroke simulation, ISK/market bots, etc.) will be
rejected without discussion.** Screen-reading overlays already live in a grey
zone of EVE Online's EULA (see the Disclaimer in [README.md](README.md));
automation is unambiguously forbidden by CCP and would put every user's
account at risk.

## Getting started

```bash
git clone <this repo>
cd TM
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r Requirements_tm.txt -r requirements-dev.txt
pytest
```

The test suite (280+ tests) is network-free and display-free (Qt `offscreen`),
so it runs anywhere — CI, SSH, containers. It must pass before any PR.

## Code conventions

Please match the existing style — consistency matters more than preference:

- **Comments are written in French**, as `#` comments (no docstrings), and
  explain the *why*, not the *what*. Every module, class and function carries
  one. Yes, even in an English-facing repo — the codebase is French-commented
  end to end.
- **Dependencies are pinned** (`==`) so the PyInstaller build stays
  reproducible. Bumping a pin is fine in a dedicated PR, after running the
  suite and a manual smoke test.
- **Cross-platform:** the tool runs on Windows (primary), macOS and Linux/X11.
  Platform-specific code goes behind `sys.platform` guards or into
  `tm/audio.py`-style backend layers — never inline OS assumptions in
  shared code. Windows behavior must remain unchanged.
- **Safety-first state machine:** the HUD must never show `ALL CLEAR` from
  stale, failed, or ambiguous data. When in doubt, show `CHECKING` and make
  failures loud. Read the *Known technical notes* in the README before
  touching `tm/monitor.py`, `tm/coordinates.py` or the mirror logic.
- **Tests:** new logic comes with tests (see `tests/` for patterns — pure
  logic is unit-tested, widget behavior is tested offscreen with mocked
  MSS/network).

## Pull request checklist

1. Fork, create a topic branch.
2. Keep the diff focused — one topic per PR.
3. `pytest` passes locally.
4. If you touched capture, coordinates, or rendering: describe your display
   setup (OS, monitors, scaling) in the PR — DPI bugs are environment-bound.
5. No new dependencies without discussion first (an issue is fine).

## Reporting bugs

Open an issue with:

- OS and display setup (scaling %, number of monitors),
- how you run it (source or packaged exe),
- the relevant lines of `threat_monitor.log`,
- what you expected vs. what happened.

For security issues, see [SECURITY.md](SECURITY.md) — do not open a public
issue.
