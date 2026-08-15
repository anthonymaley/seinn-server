# seinn server

The serving side of **seinn** (Irish: "play") — a native Apple TV and iPhone
player for video libraries served from your own hardware. This repo is the
server: a single stdlib-Python agent, an installer, a setup wizard, a health
doctor, a library converter, and a terminal UI, deployable on Linux
(systemd), macOS (launchd), or Docker.

One stdlib-Python file, one TOML config, plain HTTP on your LAN. No
dependencies to install for the agent itself — `seinn_agent.py` runs on a
bare Python 3.11+ interpreter. No account, no cloud, no telemetry.

The apps themselves (tvOS/iOS) are closed-source and distributed separately;
this server is everything they talk to.

## The pieces

| Piece | What it is |
|---|---|
| `seinn_agent.py` | The agent: listings, byte-range streaming, thumbnails, durations, watched state, delete — over plain HTTP |
| `install.sh` | One-command install; interactive wizard when run with no flags on a TTY |
| `seinn_agent.py --doctor` | Nine read-only health checks, one actionable fix line each |
| `seinn_convert.py` | Library normalizer: probes your files, converts what needs it — dry-run by default |
| `seinn_web.html` | The browser management surface: claim once, then doctor, shares, settings, convert — served by the agent at `/` |
| `seinn_tui.py` | Experimental terminal UI (parked — the browser surface is the supported experience) |
| `Dockerfile` + compose example | The same agent as a container, config-survival guaranteed |

## The two opinions

seinn is opinionated about exactly two things:

- **One format**: H.264 8-bit SDR + AAC in MP4/MOV, no B-frames — everything
  Apple silicon decodes in hardware, nothing it doesn't. B-frames alone cost
  an 11–33 s open stall on Apple TV (proven with a matched-pair experiment).
  `seinn-convert` exists so a library that isn't there yet can get there.
- **One protocol**: HTTP with byte ranges. No SMB, NFS, SFTP, or DLNA — HTTP
  seeks faster on Apple hardware than any of them and is measurable end to end.

Dependencies are deliberately *not* an opinion: the serving daemon is
stdlib-only, but tooling may depend freely (the TUI uses Textual, installed
into its own private venv).

## Get it

Clone this repo, or build the release tarball:

```sh
tools/make_release.sh
```

This produces `dist/seinn-agent-<version>.tar.gz` and a matching `.sha256` —
self-contained, installable from inside the extraction.

## Set up

Pick your platform — each guide is complete on its own:

- **[Linux (systemd)](docs/setup-linux.md)** — the primary path
- **[macOS (launchd)](docs/setup-macos.md)**
- **[Docker / compose](docs/setup-docker.md)**

And the two optional tools:

- **[The seinn TUI](docs/tui.md)** — dashboard, shares editor, converter
- **[seinn-convert](docs/convert.md)** — normalize a library to the format contract

The short version, on Linux:

```sh
sudo ./install.sh
```

The installer's last line prints `http://<server-ip>:8378/?code=XXXX-XXXX` —
one click claims the server in your browser (Docker prints the same line in
`docker logs seinn`). From there: doctor with fix lines, a real folder
picker for shares, thumbnail/delete toggles, the app token as a QR, and
`seinn-convert` with live progress. Reads stay open on the LAN; every
change needs the claim session (12 h, cookie + CSRF).

No flags on a TTY starts the interactive wizard: shares, service user,
permissions, port, delete — each answer validated for real, with the exact
fix command offered when something isn't readable or writable. Nothing is
written until you confirm a final summary; declining leaves the filesystem
untouched.

## Connect the app

Server address: `http://<server-ip>:8378`.

The auth token lives in `<prefix>/seinn-agent.toml` (the `auth_token` key —
default prefix is `/opt/seinn`). Reads and playback work without it;
progress-saving and delete require it. Enter it once in the seinn app when
adding this server.

## Troubleshooting

Start with doctor — read-only, safe against a live server:

```sh
python3 /opt/seinn/seinn_agent.py --doctor
```

It checks config parsing, share permissions, port availability, ffmpeg, the
auth token, the state database, and the service, printing one actionable
line per problem and a final `PASS`/`ISSUES` verdict. It never restarts,
rewrites, or migrates anything. Each setup guide has a troubleshooting
section for its platform's specifics.

## Design guarantees

- **Config is never overwritten.** Every surface — installer, wizard,
  Docker entrypoint, upgrades — generates config only if absent. There is
  no force path through that guard.
- **Refusals leave the filesystem exactly as found.** Validation happens
  before the first mutation.
- **Reads are open on your LAN; writes are gated.** Progress writes and
  DELETE require the token (constant-time compared). Delete is additionally
  off by default in config.
- **Every number measured.** Sustained 373–1,126 Mbps to a wired Apple TV
  4K; a 25k-file share lists in ~0.36 s.

## License

[MIT](LICENSE).
