# seinn-convert

`seinn_convert.py` is a standalone, stdlib-only companion tool that makes
"one format" a policy instead of a wall: point it at a folder and it probes
every video, reporting what it would convert to reach the codec contract
(H.264 8-bit yuv420p, `bf=0`, AAC audio, MP4/MOV container). Most libraries
need it for a fraction of their files.

## Always dry-run first

```sh
python3 seinn_convert.py /path/to/media
```

This is a census: every file probed (ffprobe), classified into a lane, with
size and time estimates. Nothing is touched.

## Apply

```sh
python3 seinn_convert.py /path/to/media --apply
```

Three lanes, chosen per file:

- **Remux** — the streams are already compliant, only the container is
  wrong: `-c copy` into MP4. Seconds per file, bit-identical streams.
- **VAAPI** — hardware re-encode (Linux with a `/dev/dri` render device),
  quality-targeted (ICQ), never bitrate-matched. The hardware probe runs
  for real before the lane is chosen; a user without render-group access
  falls to software with the reason printed.
- **x264 rescue** — software encode for anything hardware can't take.

Every conversion is verified before it replaces anything: the output is
probed for compliance, then swapped in atomically. Originals are replaced
in place (this is a normalizer, not an archiver) — the census tells you
exactly what will happen before you say `--apply`.

## Safety properties

- Dry-run is the default; `--apply` is explicit.
- A recency guard (`--min-age`, default on) skips files modified recently —
  so a live capture directory can't have an in-flight file converted under
  the writer.
- Resumable: state is kept per directory; a re-run picks up where it
  stopped. `--retry-failed` re-attempts previous failures.
- Verify-then-atomic-replace: a failed or non-compliant output never
  touches the original.

## Known cost

Converting a non-MP4 source (mkv/ts/avi/webm) renames it to `.mp4` in
place. The agent's watch/progress history is keyed by path, so a renamed
file loses its watched/resume history.

## Flags

`--help` lists everything: `--workers`, `--min-age`, `--force`,
`--retry-failed`, `--apply`.

## Convenience symlink

```sh
ln -s "$(pwd)/seinn_convert.py" /usr/local/bin/seinn-convert
```

## macOS

Software x264 only — there is no hardware lane on macOS today
(VideoToolbox is a future lane, not implemented).
