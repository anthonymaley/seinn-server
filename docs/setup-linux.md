# Setup — Linux (systemd)

The primary deployment path. One command installs the agent, generates a
config with a fresh auth token, writes and enables a systemd unit, and
starts the service.

## Prerequisites

- Linux with systemd.
- Python 3.11+ (`python3 --version`) — the agent uses `tomllib`, stdlib
  since 3.11.
- ffmpeg — optional but recommended. It powers thumbnails and duration
  badges; everything else (browsing, streaming, progress, shuffle) works
  without it, and the agent says so in its log if it's missing.
- A user for the service to run as, that can read your media (and write it,
  if you want in-app delete — off by default). Delete additionally needs
  write permission on the share itself and the auth token; the wizard
  checks both and offers to fix the permission gap.

## Install

Easiest: run it with no flags on the server and answer the prompts —

```sh
sudo ./install.sh
```

The wizard walks you through shares, service user, permissions, port, and
delete, validating each answer for real (not just `os.access`) and offering
the exact fix command when something isn't readable or writable. Nothing is
written until you confirm a final summary; declining leaves the filesystem
untouched.

For a scripted or repeatable install, the flag form:

```sh
sudo ./install.sh --root movies=/srv/media/movies
```

This installs to `/opt/seinn` by default. See `./install.sh --help` for the
full flag list (`--prefix`, `--user`, `--port`, `--root name=/abs/path` —
repeat for multiple shares, `--service-name`, `--no-service`, `--force`,
`--dry-run`, `--wizard` to force the interactive prompts even without a
TTY, `--no-wizard` to suppress them, `--with-tui` for the
[terminal UI](tui.md)).

Health check:

```sh
curl http://<server-ip>:8378/api/roots
```

Expect a JSON list of your configured roots. An empty or refused connection
means the service isn't up yet — check `journalctl -u seinn-agent -n 50`.

Re-running `install.sh` is always safe: it replaces the agent `.py` (the
upgrade artifact), but it never overwrites an existing config. The generated
config mirrors `seinn-agent.toml.example` in this repo, which is
documentation only — it is never copied onto a server as-is.

## Connect the app

Server address: `http://<server-ip>:8378`. The auth token lives in
`<prefix>/seinn-agent.toml` (the `auth_token` key). Reads and playback work
without it; progress-saving and delete require it.

## Add a share

Edit the deployed config, add one line under `[roots]`, then restart:

```sh
sudo systemctl restart seinn-agent
```

Warning: every top-level key must sit **above** `[roots]` in the file — a
key appended below it is read by TOML as another share. If your media lives
on a removable mount, consider adding `RequiresMountsFor=/your/mount` to the
`[Service]` section of the systemd unit as a hardening line.

## Upgrade

Copy the new `seinn_agent.py` over `<prefix>/seinn_agent.py` (or re-run
`install.sh`), then restart:

```sh
sudo systemctl restart seinn-agent
```

Confirm it landed:

```sh
python3 <prefix>/seinn_agent.py --version
```

The config is yours — upgrades never touch it.

## Uninstall

```sh
sudo systemctl disable --now seinn-agent
sudo rm /etc/systemd/system/seinn-agent.service && sudo systemctl daemon-reload
sudo rm -r /opt/seinn        # includes progress.db (watched history) and thumbs
```

Note: removing the prefix deletes `progress.db` — your watched/resume
history — along with the thumbnail cache. There's no undo.

## Troubleshooting

Start with doctor — read-only, safe against a live server:

```sh
python3 /opt/seinn/seinn_agent.py --doctor
```

Two likely failures for a first-time install:

- **`Permission denied` browsing a share** — doctor names the exact folder
  and prints the fix command (`sudo setfacl -R -m u:<user>:rX <path>`, or a
  `chgrp`/`chmod` fallback).
- **Connection refused** — the service hasn't started because no roots are
  configured yet; edit the config under `[roots]` and restart.

If doctor doesn't explain it:

```sh
curl http://<server-ip>:8378/api/roots
journalctl -u seinn-agent -n 50
```

## btime note

Python has no `st_birthtime` on Linux; the agent shells out to one batched
coreutils `stat -c %W` call per listing for "date added" (statx birth time).
This works on any ext4-era filesystem; if btime is unavailable the field is
null and "date added" falls back gracefully in the app.
