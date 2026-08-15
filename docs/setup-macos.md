# Setup — macOS (launchd)

`sudo ./install.sh` works exactly the same on macOS as on Linux — wizard or
flags — with launchd standing in for systemd.

## Prerequisites

```sh
brew install python ffmpeg
```

The system `/usr/bin/python3` (Command Line Tools) is too old for `tomllib`
(the agent needs Python 3.11+), so Homebrew Python is required. ffmpeg is
optional but recommended (thumbnails, duration badges).

## Install

```sh
sudo ./install.sh
```

No flags on a TTY starts the interactive wizard (shares, service user,
permissions, port, delete). Flag form for scripted installs:

```sh
sudo ./install.sh --root movies=/Volumes/Media/movies
```

The service is a **system** LaunchDaemon, `com.seinn.agent`, at
`/Library/LaunchDaemons/com.seinn.agent.plist`. Logs (both stdout and
stderr) land at `<prefix>/log/seinn-agent.log` — there's no `journalctl`
equivalent, so `tail` the file directly.

Health check:

```sh
curl http://<server-ip>:8378/api/roots
```

## Service management

launchctl equivalent of every systemctl line in the Linux guide:

| Linux | macOS |
|---|---|
| `systemctl restart seinn-agent` | `sudo launchctl kickstart -k system/com.seinn.agent` |
| `systemctl enable --now seinn-agent` | `sudo launchctl bootstrap system /Library/LaunchDaemons/com.seinn.agent.plist` |
| `systemctl disable --now seinn-agent` | `sudo launchctl bootout system/com.seinn.agent` |
| `journalctl -u seinn-agent -n 50` | `tail -n 50 <prefix>/log/seinn-agent.log` |

## Add a share

Edit `<prefix>/seinn-agent.toml`, add one line under `[roots]` (every
top-level key must sit **above** `[roots]`), then:

```sh
sudo launchctl kickstart -k system/com.seinn.agent
```

## Upgrade

Copy the new `seinn_agent.py` over `<prefix>/seinn_agent.py` (or re-run
`install.sh`), then kickstart as above. The config is never touched.

## Uninstall

```sh
sudo launchctl bootout system/com.seinn.agent
sudo rm /Library/LaunchDaemons/com.seinn.agent.plist
sudo rm -r /opt/seinn        # includes progress.db (watched history) and thumbs
```

Note: removing the prefix deletes `progress.db` — your watched/resume
history — along with the thumbnail cache. There's no undo.

## Platform notes

- "Date added" uses `st_birthtime`, native on macOS — no coreutils
  shell-out ever runs here.
- [`seinn-convert`](convert.md) on macOS always uses software x264 — there
  is no hardware encode lane (VideoToolbox is a future lane, not
  implemented today).
- Doctor works the same as on Linux, with Darwin-specific checks:

```sh
python3 /opt/seinn/seinn_agent.py --doctor
```
