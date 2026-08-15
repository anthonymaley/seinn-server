# Setup — Docker

Same agent, same config format — Docker is a third deployment shape
alongside [Linux/systemd](setup-linux.md) (the primary documented path) and
[macOS/launchd](setup-macos.md).

## Quickstart

```sh
cp docker-compose.example.yml docker-compose.yml   # then edit volumes + ROOTS
docker compose up -d --build
docker compose logs seinn | head -n 20             # first boot prints the token handoff
curl http://<server-ip>:8378/api/roots
```

## Token

First boot generates `/config/seinn-agent.toml` (host side:
`./seinn-config/seinn-agent.toml`) with a fresh `auth_token`, printed once
in the logs. Enter it in the app when adding the server. The config is
never overwritten by any later boot — same guarantee as `install.sh`.

## Permissions

The entrypoint drops from root to `PUID`/`PGID` (gosu) before the agent
starts. Set them to the host owner of your media so listings and thumbnails
work without loosening permissions.

## Delete

Needs three things lining up: the share mounted without `:ro`,
`delete_enabled = true` in the TOML, and `PUID`/`PGID` set to the host
owner of the media.

## Upgrade

Rebuild the image, `docker compose up -d --build`. Config, watched history,
and thumbnails live in the `/config` volume and survive recreation.

## Doctor in a container

```sh
docker exec <name> python3 /app/seinn_agent.py --doctor --config /config/seinn-agent.toml
```

Expected shape on a healthy live container: one FAIL
(`port: 8378 already in use — holder unknown (ss not found)`), because the
agent itself holds the port and there is no systemd inside to attribute it
to, plus a `SKIP systemd: no systemd — fine for --no-service installs`.
Both are normal here — everything else (config parse, roots, ffmpeg/ffprobe,
auth token, state_db) behaves as it would on bare metal.

## seinn-convert in a container

```sh
docker exec <name> python3 /app/seinn_convert.py /media/<share>
```

Dry-run census, same as bare metal — see the [converter guide](convert.md).
Hardware encoding (`--apply` with VAAPI) needs the `/dev/dri` device and
the host's `render` group gid — see the commented block in
`docker-compose.example.yml`.

## No registry image (yet)

There is no published image — build locally from the repo or the release
tarball (`docker build .`, echoed at the end of `tools/make_release.sh`).
