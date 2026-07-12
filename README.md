# EVE Threat Monitor

A real-time threat detection overlay for **EVE Online**. Monitors your local chat or overview pixel strip, detects hostile vs. friendly pilots, and surfaces combat intelligence via zKillboard — all in a compact, always-on-top HUD.

---

## Support the dream — fly me toward a Titan 🚀

My EVE dream is simple: one day undock a **Supercarrier**, and eventually a **Titan**. If this tool ever saved your ship (or your pod), you can help make that happen:

- Donations are **100% voluntary** — the tool is free and complete, and stays that way whether you donate or not. No perks, no priority features, no obligations.
- **In-game ISK only**, sent to the character **`JF G`** (in EVE: search the character, right-click → *Give Money*). Never send real money — that's not what this is about.
<img width="128" height="122" alt="image" src="https://github.com/user-attachments/assets/1e3a8d79-5c32-44e8-928a-eac3ef923fe3" />

Every ISK goes into the hangar fund. Fly safe o7

---

---

## ⚠️ Disclaimer — read this first

This is a personal **project / experiment**, not a commercial product, and it is **not affiliated with, endorsed by, or supported by CCP Games** in any way.

- The tool is strictly **passive**: it reads pixels on your screen, plays a sound, and displays information. It **never** interacts with the EVE client — no clicks, no keystrokes, no game automation of any kind.
- **No automation is allowed while using this tool.** Do not combine it with bots, input broadcasters, or any software that plays the game for you — automation is explicitly forbidden by CCP, and this project wants no part of it.
- Screen-reading overlays live in a **grey zone**: this tool **may or may not be compatible with the EVE Online EULA and Terms of Service**, and CCP's policy can change at any time. It has not been reviewed or approved by CCP.
- **Use it at your own risk.** You alone are responsible for what runs on your machine and on your account; the author accepts no liability for warnings, bans, or any other consequence (see also the no-warranty clause of the [MIT license](LICENSE.md)).

EVE Online and all related logos and trademarks are the property of [CCP hf.](https://www.ccpgames.com/)

---

## Features

- **Real-time threat detection** — pixel analysis of EVE's standing indicators (red = hostile, green/blue/purple = friendly)
- **Live mirror window** — scaling-aware preview of the monitored screen region at about 10 FPS
- **zKillboard integration** — enable the clipboard scan, then copy a pilot name in EVE to see danger rating, kill efficiency, playstyle, and top ships
- **20+ EVE faction themes** — Caldari, Minmatar, Amarr, Gallente, Triglavian, pirate factions, and more
- **Audio & desktop alerts** — sound and toast notification on threat detection, repeating until acknowledged
- **System tray** — Show / Hide / Quit from the tray icon's menu
- **Persistent layout** — window positions, theme, and capture region are saved automatically
- **Mixed-DPI support** — Qt selections are mapped to native capture pixels at 100%, 125%, 150%, and 200% scaling

---

## Requirements

- Windows 10 / 11 (primary, packaged target) — macOS and Linux/X11 are supported from source (see **Platform support**)
- Python 3.12+ (pinned NumPy 2.5 requires it; the dev venv runs 3.14)
- EVE Online running with the local chat or overview visible

### Python dependencies

Versions are **pinned** (`==`) so the PyInstaller build is reproducible — a silent major bump (e.g. PyQt6 7) would break the packaged executable.

```
PyQt6==6.11.0
mss==10.2.0
numpy==2.5.0
requests==2.34.2
plyer==2.1.0
```

Audio and notifications use whatever the platform provides — nothing extra to install on Windows/macOS: `winsound` (Windows stdlib), `afplay`/`osascript` (built into macOS), `paplay`/`aplay` + `notify-send` (standard on Linux desktops).

Install:

```bash
pip install -r Requirements_tm.txt
```

---

## Running from source

```bash
cd TM
python threat_monitor.py
```

---

## Platform support

| | Windows 10/11 | macOS | Linux |
|---|---|---|---|
| Status | Primary target, packaged `.exe` | From source | From source, X11 session |
| Audio alert | `winsound` | `afplay` (built-in) | `paplay` or `aplay` |
| Notifications | `plyer` | `plyer`, fallback `osascript` | `plyer`, fallback `notify-send` |
| Screen capture | mss | mss — needs the **Screen Recording** permission (System Settings → Privacy & Security) | mss — X11 only |

Platform notes:

- **macOS** — the first launch triggers the Screen Recording permission prompt; grant it to your terminal/Python, otherwise captures come back black. Everything needed for sound and toasts ships with the OS.
- **Linux** — the entry point forces Qt's `xcb` backend (`QT_QPA_PLATFORM=xcb` unless you override it): on native Wayland, clients cannot position windows absolutely, `WindowStaysOnTopHint` is ignored, and mss cannot capture the screen. Under a Wayland session the app runs via XWayland, but XWayland capture only sees other X11 windows (EVE under Wine/Proton is X11, so this usually works); an Xorg session is the safe choice. Install `pulseaudio-utils` (paplay) or `alsa-utils` (aplay) for sound and `libnotify` (notify-send) for toasts if missing.
- The HUD font falls back automatically: Consolas (Windows) → Menlo/SF Mono (macOS) → DejaVu/Ubuntu/Liberation/Noto Mono (Linux) → system fixed font.
- The pixel detector is game-UI based, not OS based — EVE's standing colors are identical on all platforms.

---

## Building the executable

Run from **inside the activated venv**, and invoke PyInstaller through the venv's
Python so it can't fall back to a global install that lacks PyQt6:

```bash
pip install -r requirements-dev.txt   # pinned PyInstaller (+ pytest)
python -m PyInstaller --clean --noconfirm threat_monitor.spec
```

Output: `dist/Eve Threat.exe`

The spec is platform-aware: on macOS/Linux it skips the Windows manifest/icon and surgical DLL picking and lets the standard PyQt6 hook bundle the right platform plugins (a proper macOS `.app` bundle with `.icns` icon is out of scope — run from source there).

> If you call the bare `pyinstaller` command and it isn't installed in the venv,
> the shell silently uses another `pyinstaller` on PATH (a different Python with
> no PyQt6) and the spec fails with `ModuleNotFoundError: No module named 'PyQt6'`.
> `python -m PyInstaller` avoids this. UPX compression is skipped automatically if
> the `upx` binary isn't on PATH (the build still succeeds, just larger).

The spec file handles:
- Bundling the icon (`threat_icon.ico`) and audio (`alert_hostile.wav`)
- Minimal Qt6 plugin set (no WebEngine, no SQL, no Multimedia)
- UPX compression
- Windows DPI + compatibility manifest injection (`app.manifest`)

---

## Project structure

```
TM/
├── threat_monitor.py       # Entry point
├── Requirements_tm.txt     # pip dependencies (pinned)
├── requirements-dev.txt    # dev tools: pytest + PyInstaller (pinned)
├── how_to.txt              # End-user guide
├── threat_config.json      # Auto-saved user config
├── threat_monitor.spec     # PyInstaller build spec
├── app.manifest            # Windows DPI + compatibility manifest
├── alert_hostile.wav       # Threat audio alert
├── threat_icon.ico         # Tray icon
│
├── tm/
│   ├── monitor.py          # Main widget & state machine
│   ├── config.py           # Config load/save (atomic writes, rotating log)
│   ├── detection.py        # NumPy pixel analysis
│   ├── coordinates.py      # Qt logical ↔ MSS native-pixel screen mapping
│   ├── audio.py            # Audio & notification abstraction
│   ├── themes.py           # Theme registry & color utilities
│   ├── qtutil.py           # Overlay window, drag mixin, screen-clamp helpers
│   ├── zkill_stats.py      # zKillboard/ESI logic (no Qt): cache, backoff, stats
│   ├── zkill_worker.py     # Bounded, cancellable lookup pool (2 active / 32 queued)
│   ├── zkill_card.py       # zKillboard popup card (QPainter rendering)
│   └── widgets/
│       ├── area_selector.py
│       ├── mirror_window.py
│       └── transparency_slider.py
│
└── tests/                  # pytest suite (headless Qt, mocked network)
```

---

## Configuration

Settings are stored in `threat_config.json` next to the executable (or script). Edited automatically — no manual changes needed. Key fields:

| Key | Description |
|-----|-------------|
| `theme` | Active theme name |
| `opacity` | Window transparency (0.2–1.0) |
| `detection_bbox` | Absolute capture region in MSS native pixels |
| `relative_bbox` | Qt logical offsets and size inside the mirror window |
| `coordinate_space_version` | Saved capture-coordinate format version |
| `mirror_bbox` | Mirror source region in MSS native pixels |
| `mirror_position` | Mirror window position |
| `win_geom` | Main window geometry |

---

## Known technical notes

- **Transparency on Windows:** (win32 only — guarded by `sys.platform`) `QT_QPA_NO_DIRECT2D=1` and `QT_D3DCREATE_MULTITHREADED=1` are set before `QApplication` is created. This disables Qt6's Direct2D backing store and forces the software rasterizer (QImage ARGB32), which is required for `WA_TranslucentBackground` to render correctly in PyInstaller builds on Windows 10/11. **Do not** use `QT_QPA_PLATFORM=windows:nodirect2d` — that sub-option is a Qt5 leftover (the separate Direct2D plugin was removed in Qt6) and PyQt6 6.11 rejects it with `Unknown option "nodirect2d"`.
- **Windows manifest:** Declares Win 8.1 compatibility (not Win10) to keep GDI compositing active instead of DirectComposition.
- **Audio replace semantics:** on macOS/Linux the external player subprocess is terminated before a new alert starts, mirroring `winsound`'s `SND_ASYNC` behaviour (a new sound replaces the current one, alerts never overlap). `stop_sound` kills the player; nothing ever blocks the Qt thread.
- **Notification fallback:** `plyer` is tried first everywhere; on macOS/Linux, if it fails once (missing `pyobjus`/`dbus` backend), the app switches permanently to the native command (`osascript` / `notify-send`) for the session. On Windows the historical behaviour is unchanged.
- **Capture safety:** The selector is scaling-aware and a selection must fit entirely on one display; cross-screen selections are rejected instead of being mapped ambiguously. After upgrading, a legacy capture may require one F2/F3 reselection on a scaled display. Monitoring starts in `CHECKING`; `ALL CLEAR` is shown only after a successful zero-threat sample. Three additional guards keep stale pixels from ever reading as safe: while the mirror's own source capture is failing, its frozen preview is never analyzed (the HUD holds `CHECKING`); re-selecting a *different* mirror region (F2) purges the old detection offsets and requires a fresh F3; and any display-layout/scale change stops monitoring visibly (`SCREEN CHG` + toast) instead of silently capturing relocated content.
- **zKillboard caching:** Fresh pilot stats are cached for 5 minutes; during an outage, a successful cached result may be reused for up to 1 hour. Failed lookups are negatively cached for 1 minute and name→id resolutions for 1 hour. Positive, name, and failure caches are capped at 512 entries. A `429` honors `Retry-After` and establishes a shared cooldown. Same-character requests are coalesced, and a shared pool permits at most two active lookups plus 32 pending; queue overflow cancels the oldest pending request. ESI / zKillboard requests reuse one HTTP session per worker thread (`requests.Session` is not guaranteed thread-safe) and send a descriptive `User-Agent` (zKillboard requires one).
- **zKill outcome messages:** `UNKNOWN PILOT` means ESI found no exact character, `NO DATA` means the character exists but zKillboard supplied no usable statistics, `RATE LIMITED` means zKillboard asked the app to wait, and `NETWORK ERROR` means ESI/zKillboard or its response was temporarily unavailable.
- **Clipboard privacy:** While zKill Scan is on, candidate pilot names are sent to CCP ESI; resolved character IDs are then sent to zKillboard. Pilot identifiers may also appear in the local rotating log. Requests include the application's descriptive contact `User-Agent`.
- **Logging:** Runtime errors are written to `threat_monitor.log` next to the executable/script (the windowed PyInstaller build has no console, so `print` output would otherwise be lost). The log rotates at 1 MB (2 backups) so it can't grow unbounded.
- **Config writes:** `threat_config.json` is written atomically (temp file + `os.replace`), so a crash mid-write can't corrupt it and wipe your settings.
- **Mirror preview:** refreshes at ~10 FPS and skips the conversion/repaint when the captured frame is unchanged, keeping the always-on overlay's CPU cost low.

## Tests

Pure logic (pixel detection, clustering, stats, ISK formatting, theme colors,
atomic config) is covered by unit tests, and the widget/state-machine behaviour
by headless integration tests (Qt `offscreen` platform — no display needed):

```bash
pip install -r requirements-dev.txt
pytest
```

The suite is network-free (ESI/zKillboard are mocked) and screen-free (MSS and
the Qt platform are faked/offscreen), so it runs anywhere without EVE or a display.

## License & community

- [MIT License](LICENSE.md) — Copyright (c) 2026 JFGel
- [Contributing guidelines](CONTRIBUTING.md) — including the hard **no-automation** rule
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security policy](SECURITY.md) — private reporting, data flows, threat model
