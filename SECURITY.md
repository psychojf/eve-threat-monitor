# Security Policy

## Supported versions

This is a hobby project / experiment. Only the latest code on the default
branch is supported — older commits and packaged builds receive no fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Preferred: use GitHub's **private vulnerability reporting**
  (*Security → Report a vulnerability* on the repository page).
- Alternative: email **jfgelinasg@gmail.com** with a description and, if
  possible, steps to reproduce.

You should get an answer within a few days. This is a one-person spare-time
project — please be patient, and give reasonable time for a fix before any
public disclosure.

## What this tool does with your data (threat model)

Knowing the data flows makes it easier to judge what is and isn't a
vulnerability:

- **Screen capture stays local.** Pixels are analyzed in memory and are never
  written to disk or sent anywhere.
- **Network calls happen only when the optional zKill clipboard scan (F5) is
  ON**, and go exclusively to CCP's ESI API and zKillboard over HTTPS:
  candidate pilot names from your clipboard are sent to ESI, and resolved
  character IDs to zKillboard, with a descriptive `User-Agent`.
- **The local log (`threat_monitor.log`) may contain pilot names** from zKill
  lookups. It stays on your machine and rotates at 1 MB.
- **`threat_config.json`** stores only screen coordinates, theme, window
  positions and opacity — no account data.
- **No credentials.** The tool never asks for, stores, or transmits EVE login
  data, tokens, or personal information.
- Dependencies are version-pinned in `Requirements_tm.txt`.

## Out of scope

- EVE Online EULA / Terms of Service compliance questions — see the
  **Disclaimer** in [README.md](README.md). This policy covers software
  security only.
- Detection accuracy (missed or false threat alerts) — those are regular bugs,
  please open a normal issue.
