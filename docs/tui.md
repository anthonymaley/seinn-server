# The seinn TUI

A full-screen terminal UI for the server: a dashboard with live health
checks, a shares editor, and the converter with progress.

## Install

```sh
sudo ./install.sh --with-tui
```

This adds `seinn` to your PATH. It installs [Textual](https://textual.textualize.io/)
into a private venv under the prefix; **the agent itself never needs it** —
everything the TUI does is also available via the wizard, the install
flags, and `--doctor`. The daemon stays stdlib-only by design.

## Run

```sh
seinn
```

Over SSH, allocate a TTY:

```sh
ssh -t <server> seinn
```

## What's in it

- **Dashboard** — agent version, service state, and the doctor's nine
  health checks, live.
- **Shares editor** — the one supported interactive way to edit `[roots]`.
  It writes the config safely: the "key appended below `[roots]` becomes a
  share" TOML trap is structurally impossible here.
- **Convert wizard** — drives [`seinn-convert`](convert.md) with a census
  first, per-file progress, and the same dry-run-first discipline.

## Uninstall

The TUI lives entirely under the install prefix (venv + launcher); removing
the prefix removes it. To drop only the TUI, delete the `seinn` launcher
from your PATH and the venv directory under the prefix.
