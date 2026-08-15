#!/usr/bin/env python3
"""seinn-convert — the "one format" policy tool.

A stranger points this at a folder: `seinn-convert /path` probes everything
and REPORTS what it would do; `--apply` converts what needs it and touches
nothing that is already optimal. It packages the project's proven corpus
lessons (B-frames, ICQ, the remux/transcode split, verify-then-replace) as a
product, standing alongside seinn_agent.py (same stdlib-only posture) but
never imported by it — this file is standalone by design.

The codec contract (CONTEXT.md Key Decisions, binding): optimal = H.264,
8-bit SDR (yuv420p), has_b_frames=0, AAC audio (or no audio), in a real
MP4/MOV container, moov at front where we wrote it. -bf 0 is the whole
point — B-frames are the proven cause of the 11-33s tvOS open stall
(2026-08-02, causal pair proof). Re-encodes are quality-targeted, never
bitrate-matched: h264_vaapi -b:v pads like CBR (+84% measured);
-rc_mode ICQ -global_quality 24-26 is the 2026-08-06 decision.

Safety invariants (non-negotiable):
  1. A source file is never deleted or modified on any failure path.
  2. A file that probes optimal is never opened for writing.
  3. Replacement is verify-then-atomic: the temp output is verified
     (parseable, h264, bf=0, duration +/-2s) before it takes the source's
     place, the replace is os.replace on the same filesystem, and the
     source's mtime is preserved onto the output.
  4. A file modified recently (default 10 min) is skipped as possibly live.
  5. Interrupt (Ctrl-C) leaves no temp files and no half-replaced sources;
     the state file makes the next run resume where this one stopped.

Out of scope: GUI, daemon/watch mode, network features, non-video files
(censused as not-video, never touched), integration into the agent process,
subtitle/attachment stream preservation beyond what -map carries, HDR
tone-mapping (10-bit sources get a plain yuv420p downconvert; the report
flags them).
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

CONVERT_VERSION = "1.0.0"

# Mirrors seinn_agent.py's VIDEO_EXTS — one definition of "video" for the
# whole system. This file is standalone by design (no import of the agent),
# so the set is duplicated deliberately rather than shared.
VIDEO_EXTS = {"mp4", "mkv", "ts", "mov", "m4v", "avi", "webm"}

MIN_AGE_DEFAULT = 600  # seconds; --min-age is given in minutes on the CLI

# Dot-prefixed so the agent's dotfile filter hides it from any browsing
# client mid-write, and so a crashed run's leftovers are recognizable.
TMP_SUFFIX = ".seinn-convert.tmp.mp4"

STATE_DIR = "/var/tmp/seinn-convert"
STATE_SAVE_EVERY = 20  # conversions between periodic state flushes

# Quality-targeted encode constants (2026-08-06 decision, reference-server
# measurement). ICQ, never -b:v: h264_vaapi -b:v pads like CBR (+84%
# measured on the corpus). Height threshold and global_quality values are
# the tuned split from that session.
VAAPI_HEIGHT_THRESHOLD = 720
VAAPI_ICQ_HIGH = 24   # height >= threshold
VAAPI_ICQ_LOW = 26    # height < threshold
X264_PRESET = "veryfast"
X264_CRF = 21

# Throughput constants for the report's time estimates — 2026-08 reference-server
# measurement basis, one box, other conditions. Benchmarks are not
# projections: the report always carries the caveat alongside these numbers.
VAAPI_REALTIME_FACTOR = 6.3
X264_REALTIME_FACTOR = 3.0
REMUX_BYTES_PER_SEC = 100 * 1024 * 1024  # ~100 MB/s, disk-bound estimate

VAAPI_DEVICE = "/dev/dri/renderD128"

FFPROBE_TIMEOUT = 30
REMUX_MIN_TIMEOUT = 600
TRANSCODE_MIN_TIMEOUT = 1800

SIGTERM_GRACE = 5.0  # seconds between SIGTERM and SIGKILL on stop


# ---------------------------------------------------------------------
# Discovery + census entries
# ---------------------------------------------------------------------

class FileEntry:
    """One discovered file. Extended in place as later steps add fields
    (probe results, verdict, plan, conversion result) — the shape every
    step hangs data off of."""

    __slots__ = (
        "relpath", "full", "size", "mtime", "atime", "verdict", "plan",
        "probe", "reason", "result", "detail",
    )

    def __init__(self, relpath, full, size, mtime, atime):
        self.relpath = relpath
        self.full = full
        self.size = size
        self.mtime = mtime
        self.atime = atime
        self.verdict = None   # not-video, skipped-recent, broken, optimal,
                               # needs-remux, needs-transcode, failed-previous,
                               # changed-underfoot
        self.plan = None      # None, "remux", "remux-audio", "transcode"
        self.probe = None     # parsed ffprobe dict or None
        self.reason = None    # human-readable note for report lines
        self.result = None    # remuxed, vaapi, x264, x264-rescue, FAILED
        self.detail = None    # extra detail for the result line


def discover(root, min_age_seconds, force, now=None):
    """Walk root, prune dot-directories, skip dotfiles and our own stale
    temp leftovers (reported separately as a cleanup note, never converted).
    Assigns the probe-free verdicts only: not-video, skipped-recent. Every
    other file is left with verdict=None for the classifier to fill in."""
    if now is None:
        now = time.time()
    entries = []
    stale_temps = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname.startswith("."):
                if fname.endswith(TMP_SUFFIX):
                    stale_temps.append(os.path.join(cur, fname))
                continue
            full = os.path.join(cur, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            relpath = os.path.relpath(full, root)
            ext = os.path.splitext(fname)[1].lstrip(".").lower()
            entry = FileEntry(relpath, full, st.st_size, st.st_mtime, st.st_atime)
            if ext not in VIDEO_EXTS:
                entry.verdict = "not-video"
            elif not force and (now - st.st_mtime) < min_age_seconds:
                entry.verdict = "skipped-recent"
            entries.append(entry)
    return entries, stale_temps


# ---------------------------------------------------------------------
# Census printing
# ---------------------------------------------------------------------

# Order matters for the printed report — mirrors the spec header's sample.
CENSUS_BUCKETS = [
    "optimal", "needs-remux", "needs-transcode", "skipped-recent",
    "failed-previous", "broken", "not-video",
]

CENSUS_LABELS = {
    "optimal": "untouched",
    "needs-remux": None,   # filled with time estimate later
    "needs-transcode": None,
    "skipped-recent": "modified < {min_age} min ago (--force to include)",
    "failed-previous": "see --retry-failed",
    "broken": "ffprobe cannot read it",
    "not-video": "ignored",
}


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def census_data(entries, root, min_age_minutes, probed_count, cached_count,
                vaapi_active, remux_audio_count=0):
    """Pure computation behind the census report: same bucket order/skip
    rule as print_census, same note text, no printing. Returns a dict:
    {root, probed, cached, lane, lane_factor, buckets: [...], caveat}."""
    by_verdict = {}
    for e in entries:
        by_verdict.setdefault(e.verdict, []).append(e)

    remux_bytes = sum(e.size for e in by_verdict.get("needs-remux", []))
    remux_secs = remux_bytes / REMUX_BYTES_PER_SEC

    transcode_entries = by_verdict.get("needs-transcode", [])
    transcode_secs_source = sum(
        (e.probe.get("duration") or 0) if e.probe else 0
        for e in transcode_entries)
    lane_factor = VAAPI_REALTIME_FACTOR if vaapi_active else X264_REALTIME_FACTOR
    lane_name = "vaapi" if vaapi_active else "x264"
    transcode_est = (transcode_secs_source / lane_factor
                      if lane_factor else 0)

    buckets = []
    for bucket in CENSUS_BUCKETS:
        items = by_verdict.get(bucket, [])
        if not items and bucket not in ("optimal", "not-video"):
            continue
        count = len(items)
        size_bytes = sum(e.size for e in items)
        if bucket == "optimal":
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": None, "note": "untouched"})
        elif bucket == "needs-remux":
            note = "(copy, disk-bound est.)"
            if remux_audio_count:
                note = f"(copy, disk-bound est.; {remux_audio_count} audio-only)"
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": remux_secs, "note": note})
        elif bucket == "needs-transcode":
            note = f"({lane_name} lane, est. {lane_factor}x realtime)"
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": transcode_est, "note": note})
        elif bucket == "skipped-recent":
            buckets.append({
                "name": bucket, "count": count, "bytes": size_bytes,
                "est_seconds": None,
                "note": f"modified < {min_age_minutes} min ago (--force to include)"})
        elif bucket == "failed-previous":
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": None, "note": "see --retry-failed"})
        elif bucket == "broken":
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": None, "note": "ffprobe cannot read it"})
        elif bucket == "not-video":
            buckets.append({"name": bucket, "count": count, "bytes": size_bytes,
                             "est_seconds": None, "note": "ignored"})

    caveat = ("Estimates are extrapolations from measured throughput on one box "
              "under other conditions (vaapi ~6.3x, x264 ~3x realtime, remux "
              "disk-bound); treat as rough.")

    return {
        "root": root, "probed": probed_count, "cached": cached_count,
        "lane": lane_name, "lane_factor": lane_factor,
        "buckets": buckets, "caveat": caveat,
    }


def print_census(entries, root, min_age_minutes, probed_count, cached_count,
                  vaapi_active, remux_audio_count=0):
    data = census_data(entries, root, min_age_minutes, probed_count,
                        cached_count, vaapi_active, remux_audio_count)

    print(f"seinn-convert census of {data['root']} "
          f"({data['probed']} files probed, {data['cached']} cached)")

    shows_size = {"optimal", "needs-remux", "needs-transcode"}
    shows_time = {"needs-remux", "needs-transcode"}
    for b in data["buckets"]:
        unit = "file" if b["count"] == 1 else "files"
        parts = [f"  {b['name']:<16}{b['count']:>5} {unit:<6}"]
        if b["name"] in shows_size:
            parts.append(f"{fmt_bytes(b['bytes']):<8}")
        if b["name"] in shows_time:
            parts.append(f"~{fmt_duration(b['est_seconds']):<7}")
        if b["note"]:
            parts.append(b["note"])
        print(" ".join(parts).rstrip())

    print(data["caveat"])

    by_verdict = {}
    for e in entries:
        by_verdict.setdefault(e.verdict, []).append(e)
    return by_verdict


# ---------------------------------------------------------------------
# ffprobe layer and classifier
# ---------------------------------------------------------------------

STREAM_KEYS = {"codec_type", "codec_name", "has_b_frames", "pix_fmt", "height"}


def _split_probe_blocks(text):
    """ffprobe -of default=noprint_wrappers=1 emits flat key=value lines,
    NOT CSV in request order (ffprobe's canonical field order is its own,
    never the order you asked for — measured: codec_name printed BEFORE
    codec_type in this build) — so every value here is parsed by key, never
    by position, and the block splitter cannot assume any one field is
    first. Instead: a stream block has no explicit delimiter, but it also
    never repeats a field name — so the first key that's already present in
    the block being built signals the start of the next stream's block."""
    blocks = []
    current = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key not in STREAM_KEYS:
            continue  # format=... fields (format_name, duration) parsed separately
        if key in current:
            blocks.append(current)
            current = {}
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def probe_file(path):
    """Single ffprobe call per the spec's field list. Returns a dict:
    {format_name, duration, video: {codec, has_b_frames, pix_fmt, height},
    audio: [codec, ...]} or None on any probe failure (rc != 0, timeout,
    unparseable, no video stream). format=... fields appear as bare
    key=value lines outside any stream block (no codec_type key), so they
    are captured separately rather than folded into the block splitter."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration:stream=codec_type,codec_name,"
             "has_b_frames,pix_fmt,height",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None

    format_fields = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in ("format_name", "duration"):
            format_fields[key] = value

    blocks = _split_probe_blocks(res.stdout)
    video = None
    audio = []
    for block in blocks:
        ctype = block.get("codec_type")
        if ctype == "video" and video is None:
            has_bf_raw = block.get("has_b_frames", "0")
            try:
                has_bf = int(has_bf_raw)
            except ValueError:
                has_bf = 0
            height_raw = block.get("height", "")
            try:
                height = int(height_raw)
            except ValueError:
                height = None
            video = {
                "codec": block.get("codec_name"),
                "has_b_frames": has_bf,
                "pix_fmt": block.get("pix_fmt"),
                "height": height,
            }
        elif ctype == "audio":
            audio.append(block.get("codec_name"))

    if video is None:
        return None

    duration = None
    if "duration" in format_fields:
        try:
            duration = float(format_fields["duration"])
        except ValueError:
            duration = None

    return {
        "format_name": format_fields.get("format_name", ""),
        "duration": duration,
        "video": video,
        "audio": audio,
    }


def classify(probe):
    """Verdict table from the spec header, exactly. Returns (verdict, plan)
    where plan is one of 'remux', 'remux-audio', 'transcode', or None."""
    if probe is None:
        return "broken", None

    fmt = probe["format_name"] or ""
    video = probe["video"]
    audio = probe["audio"]

    is_h264 = video["codec"] == "h264"
    is_420p = video["pix_fmt"] == "yuv420p"
    no_bframes = video["has_b_frames"] == 0
    all_aac = all(a == "aac" for a in audio)  # vacuously true if no audio

    # mov,mp4,m4a,... is ffmpeg's compound demuxer name for the mp4 family —
    # corpus containers lie (a .mp4 extension that actually probes mpegts is
    # NOT optimal and NOT a clean remux target the normal way; it still IS
    # remuxable, see below), so this checks the probed format, never the
    # filename extension.
    is_mp4_family = "mp4" in fmt or "mov" in fmt

    if is_h264 and is_420p and no_bframes and all_aac:
        if is_mp4_family:
            return "optimal", None
        # h264/yuv420p/bf0/aac but wrong container (mkv, ts, avi, webm, or
        # mp4-named-but-actually-mpegts) — cheap remux, -c copy fixes it.
        return "needs-remux", "remux"

    if is_h264 and is_420p and no_bframes and not all_aac:
        # Video already meets contract; only audio needs fixing. Cheap
        # (audio-only encode). Verdict stays "needs-remux" (three action
        # buckets in the census) — the report adds a "(audio)" qualifier by
        # inspecting plan == "remux-audio" on the entries in that bucket.
        return "needs-remux", "remux-audio"

    # Anything else: B-frames present, non-h264 video, non-yuv420p. Full
    # re-encode required.
    return "needs-transcode", "transcode"


# ---------------------------------------------------------------------
# State file: probe cache, failure memory, done markers
# ---------------------------------------------------------------------
#
# One JSON file per target root, keyed by sha256(realpath)[:16] so the same
# path always maps to the same file without a lookup table. Key inside the
# file is "(relpath, size, mtime)" joined into a string — a file whose
# size or mtime changed is a fresh key automatically, which is what makes
# failure memory and the probe cache both self-invalidating on any real
# change to the file. The file is advisory: deleting it is always safe, it
# only costs re-probing (never a correctness dependency).

def state_path_for(root):
    real = os.path.realpath(root)
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return os.path.join(STATE_DIR, f"{digest}.json")


def entry_key(relpath, size, mtime):
    # mtime as int: sub-second jitter across filesystems/tools would
    # otherwise defeat the cache on files nobody actually touched.
    return f"{relpath}|{size}|{int(mtime)}"


def load_state(root):
    path = state_path_for(root)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "entries" not in data:
            raise ValueError("unexpected shape")
        return data
    except FileNotFoundError:
        return {"entries": {}}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"seinn-convert: state file {path} unreadable ({exc}) — "
              f"starting fresh (advisory data only, nothing lost but cache)",
              flush=True)
        return {"entries": {}}


def save_state_atomic(root, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = state_path_for(root)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StateStore:
    """Wraps the raw state dict with a lock (workers write concurrently in
    Step 6) and periodic-save bookkeeping."""

    def __init__(self, root):
        self.root = root
        self.state = load_state(root)
        self.lock = threading.Lock()
        self._dirty_count = 0

    def get(self, relpath, size, mtime):
        with self.lock:
            return self.state["entries"].get(entry_key(relpath, size, mtime))

    def put_probe(self, relpath, size, mtime, verdict, plan, probe):
        with self.lock:
            self.state["entries"][entry_key(relpath, size, mtime)] = {
                "kind": "probe", "verdict": verdict, "plan": plan, "probe": probe,
            }

    def put_failure(self, relpath, size, mtime, stage, rc, stderr_tail):
        with self.lock:
            key = entry_key(relpath, size, mtime)
            prior = self.state["entries"].get(key, {})
            attempts = prior.get("attempts", 0) + 1 if prior.get("kind") == "failed" else 1
            self.state["entries"][key] = {
                "kind": "failed", "when": time.time(), "stage": stage,
                "rc": rc, "stderr_tail": stderr_tail, "attempts": attempts,
            }

    def put_done(self, relpath, size, mtime, out_relpath, out_size, out_mtime, result):
        with self.lock:
            self.state["entries"][entry_key(relpath, size, mtime)] = {
                "kind": "done", "out_relpath": out_relpath,
                "out_size": out_size, "out_mtime": out_mtime, "result": result,
            }

    def maybe_flush(self, force=False):
        with self.lock:
            self._dirty_count += 1
            due = force or self._dirty_count >= STATE_SAVE_EVERY
            if due:
                self._dirty_count = 0
                snapshot = json.loads(json.dumps(self.state))
        if due:
            save_state_atomic(self.root, snapshot)

    def flush_now(self):
        with self.lock:
            snapshot = json.loads(json.dumps(self.state))
        save_state_atomic(self.root, snapshot)


def classify_with_cache(entry, store, retry_failed):
    """Fills entry.verdict/plan/probe, using the state store's probe cache
    and failure memory. Returns True if a fresh ffprobe was run, False if
    served from cache (for the census's 'N probed, M cached' line)."""
    cached = store.get(entry.relpath, entry.size, entry.mtime)
    if cached is not None:
        if cached["kind"] == "failed" and cached.get("stage") == "probe-failed":
            # Probe failures stay "broken" forever (never re-probed, never
            # gated by --retry-failed — a corrupt file isn't going to start
            # parsing on a retry). Distinct from a failed CONVERT, which
            # reroutes through failed-previous below.
            entry.verdict = "broken"
            entry.plan = None
            return False
        if cached["kind"] == "failed" and not retry_failed:
            entry.verdict = "failed-previous"
            entry.plan = None
            entry.reason = cached.get("stage")
            return False
        if cached["kind"] == "probe":
            entry.verdict = cached["verdict"]
            entry.plan = cached["plan"]
            entry.probe = cached["probe"]
            return False
        # cached["kind"] == "done" (or "failed" with --retry-failed): done
        # markers are a fast-path only — re-probe to confirm, verdicts
        # always win over markers (spec: a marker is not a trust anchor).

    probe = probe_file(entry.full)
    verdict, plan = classify(probe)
    entry.probe = probe
    entry.verdict = verdict
    entry.plan = plan
    if verdict == "broken":
        store.put_failure(entry.relpath, entry.size, entry.mtime,
                           "probe-failed", None, None)
    else:
        store.put_probe(entry.relpath, entry.size, entry.mtime, verdict, plan, probe)
    return True


# ---------------------------------------------------------------------
# VAAPI availability probe
# ---------------------------------------------------------------------
#
# VAAPI availability is TESTED, not assumed: existence of /dev/dri/renderD128
# says nothing about whether this user/process can actually open and use it.
# On the reference server this probe FAILS for a user outside render/video and
# that is correct behavior, not a bug — the tool falls to software exactly
# as a stranger's box without working VAAPI would.
#
# SEINN_CONVERT_TEST_FAKE_VAAPI=1 forces this to report available without
# touching real hardware, and forces vaapi encode attempts to run `false`
# instead of ffmpeg — the only host-independent test of the rescue rule
# (same doctrine as seinn_agent.py's --sim-* flags). TEST-ONLY, loudly
# flagged whenever active.
FAKE_VAAPI_ENV = "SEINN_CONVERT_TEST_FAKE_VAAPI"


def vaapi_available():
    # VAAPI is the only hardware lane today; on macOS this probe always fails
    # and the software x264 lane carries everything. VideoToolbox is a future
    # lane, not a fallback gap.
    if os.environ.get(FAKE_VAAPI_ENV) == "1":
        print("seinn-convert: TEST-ONLY SEINN_CONVERT_TEST_FAKE_VAAPI=1 active "
              "— faking VAAPI available and routing vaapi jobs to `false`. "
              "Do not use this outside tests.", flush=True)
        return True, "test-fake"

    if not os.path.exists(VAAPI_DEVICE):
        return False, f"{VAAPI_DEVICE} does not exist"

    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-vaapi_device", VAAPI_DEVICE,
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe failed to run ({exc})"
    if res.returncode != 0:
        stderr_tail = res.stderr.decode("utf-8", "replace")[-300:].strip()
        return False, f"probe rc={res.returncode}: {stderr_tail}"
    return True, "live 1s encode probe succeeded"


# ---------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------

def build_remux_cmd(entry, tmp_path):
    is_mpegts_source = "mpegts" in (entry.probe["format_name"] or "")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", entry.full,
           "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    if is_mpegts_source:
        # TS carries AAC as ADTS, which the MP4 muxer rejects outright
        # (measured on the capture pipeline). Only applied on TS sources —
        # never "just in case" on containers that don't need it.
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", tmp_path]
    return cmd


def build_remux_audio_cmd(entry, tmp_path):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", entry.full,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-f", "mp4", tmp_path]
    return cmd


def build_vaapi_cmd(entry, tmp_path):
    height = (entry.probe["video"].get("height") or 0)
    quality = VAAPI_ICQ_HIGH if height >= VAAPI_HEIGHT_THRESHOLD else VAAPI_ICQ_LOW
    audio_all_aac = all(a == "aac" for a in entry.probe["audio"])
    audio_args = ["-c:a", "copy"] if audio_all_aac else ["-c:a", "aac", "-b:a", "192k"]
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y",
           "-vaapi_device", VAAPI_DEVICE, "-i", entry.full,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "format=nv12,hwupload",
           "-c:v", "h264_vaapi", "-rc_mode", "ICQ",
           "-global_quality", str(quality), "-bf", "0"]
    cmd += audio_args
    cmd += ["-movflags", "+faststart", "-f", "mp4", tmp_path]
    return cmd


def build_x264_cmd(entry, tmp_path, audio_only_fallback=False):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", entry.full]
    if audio_only_fallback:
        cmd += ["-map", "0:v:0", "-an"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(X264_CRF),
            "-bf", "0", "-pix_fmt", "yuv420p"]
    if not audio_only_fallback:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", tmp_path]
    return cmd


# ---------------------------------------------------------------------
# Verify-then-atomic-replace
# ---------------------------------------------------------------------

def verify_output(tmp_path, source_duration):
    """ffprobe the temp: parses, video h264, has_b_frames==0, duration
    within +/-2s of the source's probed duration (if source duration was
    unprobeable, verify parse+bf only and flag it), size > 0. Returns
    (ok, note)."""
    try:
        size = os.path.getsize(tmp_path)
    except OSError:
        return False, "temp output missing"
    if size <= 0:
        return False, "temp output is zero bytes"

    probe = probe_file(tmp_path)
    if probe is None:
        return False, "temp output does not parse"
    if probe["video"]["codec"] != "h264":
        return False, f"temp output video codec is {probe['video']['codec']!r}, not h264"
    if probe["video"]["has_b_frames"] != 0:
        return False, "temp output has_b_frames != 0"

    if source_duration is None:
        return True, "source duration unprobeable — verified parse+bf only"
    out_duration = probe["duration"]
    if out_duration is None:
        return False, "temp output duration unprobeable"
    if abs(out_duration - source_duration) > 2.0:
        return False, (f"duration drift {out_duration - source_duration:+.1f}s "
                        f"exceeds +/-2s")
    return True, None


def replace_verified(entry, tmp_path):
    """os.utime mtime preservation, then the extension-aware atomic
    replace. Returns (final_path, note)."""
    os.utime(tmp_path, (entry.atime, entry.mtime))

    root, _ext = os.path.splitext(entry.full)
    final_path = root + ".mp4"

    if final_path == entry.full:
        os.replace(tmp_path, entry.full)
        return final_path, None
    # Different extension (mkv/ts/avi/webm input): the unlink of the
    # source happens only AFTER the verified output exists at its final
    # name — never delete-then-write.
    os.replace(tmp_path, final_path)
    os.unlink(entry.full)
    return final_path, ("renamed from a different extension — the agent's "
                         "watch-history is keyed by path, so this file's "
                         "progress/watched history resets")


def timeout_for(kind, duration):
    duration = duration or 0
    if kind == "remux":
        return max(REMUX_MIN_TIMEOUT, duration * 4)
    return max(TRANSCODE_MIN_TIMEOUT, duration * 10)


class Interrupted(Exception):
    pass


def run_ffmpeg_job(cmd, timeout, stop_event, active_procs, procs_lock):
    """Runs cmd, tracking the Popen so the signal handler can stop it.
    Returns (rc, stderr_tail). Raises Interrupted if stop_event is already
    set before the process is even launched."""
    if stop_event.is_set():
        raise Interrupted()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)
    except OSError as exc:
        return None, str(exc)
    with procs_lock:
        active_procs.add(proc)
    try:
        try:
            _out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
            return None, "timed out"
        stderr_tail = (err or b"").decode("utf-8", "replace")[-500:]
        return proc.returncode, stderr_tail
    finally:
        with procs_lock:
            active_procs.discard(proc)


def _emit_progress(progress, event, entry):
    """Calls progress(event, entry), swallowing any exception it raises: a
    broken observer must never break a conversion mid-replace. May run on
    a ThreadPoolExecutor worker thread (see apply_all/convert_one
    docstrings) — the callback owns its own thread-safety."""
    if progress is None:
        return
    try:
        progress(event, entry)
    except Exception as exc:
        print(f"seinn-convert: WARNING progress callback raised {exc!r} — continuing",
              flush=True)


def convert_one(entry, store, vaapi_ok, stop_event, active_procs, procs_lock,
                 *, progress=None):
    """Runs the full plan for one entry: build cmd(s), run, verify, replace
    or clean up on failure. Mutates entry.result/detail. Never raises except
    Interrupted (propagated so the pool stops promptly).

    progress, if given, is called as progress("file-start", entry) right
    after the changed-underfoot re-stat passes (before the ffmpeg job
    launches) and progress("file-done", entry) once entry.result/detail are
    final. May be called from a ThreadPoolExecutor worker thread (apply_all
    parallelizes software transcodes) — the callback owns its own
    thread-safety (e.g. Textual's call_from_thread). Exceptions raised by
    progress are caught and logged, never allowed to interrupt the
    conversion."""
    # Re-stat immediately before starting: if size or mtime changed since
    # the census, the file may be a live write in progress — stronger than
    # any directory-name heuristic and it cannot lie.
    try:
        st = os.stat(entry.full)
    except OSError:
        entry.result = "FAILED stat"
        entry.detail = "source vanished before job start"
        _emit_progress(progress, "file-done", entry)
        return
    if st.st_size != entry.size or int(st.st_mtime) != int(entry.mtime):
        entry.verdict = "changed-underfoot"
        entry.result = None
        entry.detail = "size/mtime changed since census — skipped, will re-probe next run"
        _emit_progress(progress, "file-done", entry)
        return

    _emit_progress(progress, "file-start", entry)

    tmp_path = os.path.join(os.path.dirname(entry.full),
                             "." + os.path.basename(entry.full) + TMP_SUFFIX)
    source_duration = entry.probe.get("duration") if entry.probe else None

    def cleanup_tmp():
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    def fail(stage, rc, stderr_tail):
        cleanup_tmp()
        store.put_failure(entry.relpath, entry.size, entry.mtime, stage, rc, stderr_tail)
        entry.result = f"FAILED {stage}"
        entry.detail = stderr_tail

    try:
        if entry.plan == "remux":
            cmd = build_remux_cmd(entry, tmp_path)
            rc, stderr_tail = run_ffmpeg_job(cmd, timeout_for("remux", source_duration),
                                              stop_event, active_procs, procs_lock)
            if rc != 0:
                fail("remux", rc, stderr_tail)
                return
            ok, note = verify_output(tmp_path, source_duration)
            if not ok:
                fail("remux-verify", None, note)
                return
            final_path, rename_note = replace_verified(entry, tmp_path)
            entry.result = "remuxed"
            entry.detail = rename_note or note
            _record_done(entry, store, final_path)
            return

        if entry.plan == "remux-audio":
            cmd = build_remux_audio_cmd(entry, tmp_path)
            rc, stderr_tail = run_ffmpeg_job(cmd, timeout_for("remux", source_duration),
                                              stop_event, active_procs, procs_lock)
            if rc != 0:
                fail("remux-audio", rc, stderr_tail)
                return
            ok, note = verify_output(tmp_path, source_duration)
            if not ok:
                fail("remux-audio-verify", None, note)
                return
            final_path, rename_note = replace_verified(entry, tmp_path)
            entry.result = "remuxed"
            entry.detail = rename_note or note
            _record_done(entry, store, final_path)
            return

        if entry.plan == "transcode":
            _convert_transcode(entry, store, tmp_path, source_duration, vaapi_ok,
                                stop_event, active_procs, procs_lock, fail, cleanup_tmp)
            return

        entry.result = "FAILED unknown-plan"
        entry.detail = repr(entry.plan)
    finally:
        # entry.result is set on every path above except Interrupted (raised
        # by run_ffmpeg_job before any fail()/success assignment) — so this
        # naturally fires file-done only when the result is truly final, and
        # is skipped when the pool is being torn down mid-job.
        if entry.result is not None:
            _emit_progress(progress, "file-done", entry)


def _record_done(entry, store, final_path):
    try:
        st = os.stat(final_path)
        out_relpath = os.path.relpath(final_path, os.path.dirname(entry.full) or ".")
        store.put_done(entry.relpath, entry.size, entry.mtime,
                        final_path, st.st_size, st.st_mtime, entry.result)
    except OSError:
        pass


def _convert_transcode(entry, store, tmp_path, source_duration, vaapi_ok,
                        stop_event, active_procs, procs_lock, fail, cleanup_tmp):
    timeout = timeout_for("transcode", source_duration)

    if vaapi_ok:
        cmd = build_vaapi_cmd(entry, tmp_path)
        if os.environ.get(FAKE_VAAPI_ENV) == "1":
            cmd = ["false"]
        rc, stderr_tail = run_ffmpeg_job(cmd, timeout, stop_event, active_procs, procs_lock)
        if rc == 0:
            ok, note = verify_output(tmp_path, source_duration)
            if ok:
                final_path, rename_note = replace_verified(entry, tmp_path)
                entry.result = "vaapi"
                entry.detail = rename_note or note
                _record_done(entry, store, final_path)
                return
            stderr_tail = note
        # Rescue rule: any VAAPI job failure (nonzero rc, including the
        # rc=139 segfault class, or a failed verify) falls through to the
        # software path automatically for this file, once, before the
        # failure is recorded.
        cleanup_tmp()
        _run_x264(entry, store, tmp_path, source_duration, stop_event,
                  active_procs, procs_lock, fail, cleanup_tmp, rescue=True)
        return

    _run_x264(entry, store, tmp_path, source_duration, stop_event, active_procs,
              procs_lock, fail, cleanup_tmp, rescue=False)


def _run_x264(entry, store, tmp_path, source_duration, stop_event, active_procs,
              procs_lock, fail, cleanup_tmp, rescue):
    timeout = timeout_for("transcode", source_duration)
    stage = "x264-rescue" if rescue else "x264"

    cmd = build_x264_cmd(entry, tmp_path, audio_only_fallback=False)
    rc, stderr_tail = run_ffmpeg_job(cmd, timeout, stop_event, active_procs, procs_lock)
    if rc != 0:
        # dump_fix7 pattern: an unidentifiable audio stream can make audio
        # mapping fail outright even though video is fine — retry video-only.
        cleanup_tmp()
        cmd2 = build_x264_cmd(entry, tmp_path, audio_only_fallback=True)
        rc2, stderr_tail2 = run_ffmpeg_job(cmd2, timeout, stop_event, active_procs, procs_lock)
        if rc2 != 0:
            fail(stage, rc2, stderr_tail2)
            return None
        stderr_tail = None

    ok, note = verify_output(tmp_path, source_duration)
    if not ok:
        fail(f"{stage}-verify", None, note)
        return None
    final_path, rename_note = replace_verified(entry, tmp_path)
    entry.result = stage
    entry.detail = rename_note or note
    _record_done(entry, store, final_path)
    return stage


# ---------------------------------------------------------------------
# Concurrency + signal handling
# ---------------------------------------------------------------------
#
# Concurrency rule (measured, not assumed):
#   - Remuxes are I/O-bound on rotational USB disks (random access ~800x
#     the cost of sequential) — always sequential, regardless of --workers.
#   - VAAPI is a single engine — when the VAAPI lane is active, transcodes
#     run sequentially through it; --workers is ignored for them, and the
#     report says so rather than silently serializing.
#   - Software transcodes (no VAAPI, or rescues) run up to --workers
#     parallel ffmpeg processes. Rescue jobs join the same pool.
# State-file writes are serialized behind StateStore's own lock; workers
# are ThreadPoolExecutor threads each owning one subprocess — stdlib, no
# multiprocessing (ffmpeg is already an external process, so threads are
# enough to hide Python's GIL entirely here).

class StopController:
    """Owns the stop_event and the set of in-flight ffmpeg Popen objects so
    the signal handler can reach them. One SIGTERM/SIGINT means 'stop
    launching new jobs, then grace-then-kill the ones in flight' — a naive
    handler that just sets a flag would leave a +faststart rewrite running
    for minutes after Ctrl-C."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.active_procs = set()
        self.procs_lock = threading.Lock()
        self._second_signal = threading.Event()

    def handle_signal(self, signum, _frame):
        if self.stop_event.is_set():
            # Second SIGINT during grace -> kill immediately.
            self._second_signal.set()
            self._kill_all()
            return
        print(f"\nseinn-convert: signal {signum} received — stopping new jobs, "
              f"grace period for in-flight ffmpeg...", flush=True)
        self.request_stop()

    def request_stop(self):
        """Print-free stop trigger: sets stop_event and terminates in-flight
        ffmpeg jobs (grace-then-kill). Public so a caller with no signal
        handler to piggyback on — the TUI's cancel path — can invoke the
        same stop machinery as handle_signal, without the signal-specific
        print. handle_signal calls this after printing its own message."""
        self.stop_event.set()
        self._terminate_all()

    def _snapshot_procs(self):
        with self.procs_lock:
            return list(self.active_procs)

    def _terminate_all(self):
        procs = self._snapshot_procs()
        for p in procs:
            try:
                p.terminate()  # SIGTERM: "finish gracefully" — we don't wait for that
            except OSError:
                pass
        deadline = time.time() + SIGTERM_GRACE
        while time.time() < deadline and not self._second_signal.is_set():
            if all(p.poll() is not None for p in procs):
                break
            time.sleep(0.1)
        self._kill_all()

    def _kill_all(self):
        for p in self._snapshot_procs():
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
        for p in self._snapshot_procs():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # confirm by PID: poll() returning non-None means the process is
        # actually reaped, not just signaled.
        for p in self._snapshot_procs():
            if p.poll() is None:
                print(f"seinn-convert: WARNING pid {p.pid} did not die", flush=True)


def apply_all(entries, store, workers, vaapi_ok, stop_ctrl, *, progress=None):
    """Runs remux/remux-audio jobs sequentially, then transcode jobs either
    sequentially (VAAPI active) or up to `workers` parallel (software-only).
    Mutates entries in place. Returns True if interrupted.

    progress, if given, is passed straight through to each convert_one call
    (see its docstring for the event contract). When software transcodes
    run in the ThreadPoolExecutor branch below, progress is called from
    worker threads, not the calling thread — the callback owns its own
    thread-safety. main() passes nothing, so progress stays None and the
    CLI path is unchanged."""
    remux_jobs = [e for e in entries if e.plan in ("remux", "remux-audio")]
    transcode_jobs = [e for e in entries if e.plan == "transcode"]

    interrupted = False

    for e in remux_jobs:
        if stop_ctrl.stop_event.is_set():
            interrupted = True
            break
        convert_one(e, store, vaapi_ok, stop_ctrl.stop_event,
                    stop_ctrl.active_procs, stop_ctrl.procs_lock, progress=progress)
        store.maybe_flush()

    if not interrupted and transcode_jobs:
        if vaapi_ok:
            if workers > 1:
                print(f"seinn-convert: VAAPI is a single engine — "
                      f"--workers {workers} ignored for transcodes, running sequentially",
                      flush=True)
            for e in transcode_jobs:
                if stop_ctrl.stop_event.is_set():
                    interrupted = True
                    break
                convert_one(e, store, vaapi_ok, stop_ctrl.stop_event,
                            stop_ctrl.active_procs, stop_ctrl.procs_lock, progress=progress)
                store.maybe_flush()
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {}
                for e in transcode_jobs:
                    if stop_ctrl.stop_event.is_set():
                        interrupted = True
                        break
                    fut = pool.submit(convert_one, e, store, vaapi_ok,
                                       stop_ctrl.stop_event, stop_ctrl.active_procs,
                                       stop_ctrl.procs_lock, progress=progress)
                    futures[fut] = e
                for fut in concurrent.futures.as_completed(futures):
                    e = futures[fut]
                    try:
                        fut.result()
                    except Interrupted:
                        interrupted = True
                    store.maybe_flush()
                if stop_ctrl.stop_event.is_set():
                    interrupted = True

    store.flush_now()
    return interrupted or stop_ctrl.stop_event.is_set()


# ---------------------------------------------------------------------
# Final results report (--apply)
# ---------------------------------------------------------------------

def print_final_report(entries, elapsed, vaapi_ok, vaapi_reason):
    converted = [e for e in entries if e.result and not e.result.startswith("FAILED")]
    failed = [e for e in entries if e.result and e.result.startswith("FAILED")]
    skipped_underfoot = [e for e in entries if e.verdict == "changed-underfoot"]

    print()
    print(f"seinn-convert results ({'vaapi' if vaapi_ok else 'software'} lane: {vaapi_reason})")
    for e in converted:
        note = f" — {e.detail}" if e.detail else ""
        print(f"  {e.relpath:<50} {e.result}{note}")
    for e in failed:
        note = f" — {e.detail}" if e.detail else ""
        print(f"  {e.relpath:<50} {e.result}{note}")
    for e in skipped_underfoot:
        print(f"  {e.relpath:<50} changed-underfoot — {e.detail}")

    bytes_before = sum(e.size for e in converted)
    bytes_after = 0
    for e in converted:
        try:
            root, _ = os.path.splitext(e.full)
            final_path = root + ".mp4"
            bytes_after += os.path.getsize(final_path)
        except OSError:
            pass

    print(f"totals: {len(converted)} converted, {len(failed)} failed, "
          f"{len(skipped_underfoot)} changed-underfoot")
    print(f"bytes before: {fmt_bytes(bytes_before)}  after: {fmt_bytes(bytes_after)}  "
          f"elapsed: {fmt_duration(elapsed)}")

    return len(failed) > 0


# ---------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="seinn-convert",
        description="Probe a folder of video files and report (or apply) "
                     "conversions to the project's 'one format' contract: "
                     "H.264 8-bit yuv420p, bf=0, AAC audio, MP4/MOV container.")
    ap.add_argument("path", help="directory to scan")
    ap.add_argument("--apply", action="store_true",
                     help="convert what needs it (default: dry-run census only)")
    ap.add_argument("--workers", type=int, default=1,
                     help="parallel software transcodes (default 1; ignored "
                          "for remuxes and for VAAPI transcodes, both always sequential)")
    ap.add_argument("--min-age", type=int, default=10,
                     help="minutes; files modified more recently are skipped "
                          "as possibly still-writing (default 10)")
    ap.add_argument("--force", action="store_true",
                     help="lift the recency guard (prints a warning — only "
                          "use this if you are certain nothing is writing to the folder)")
    ap.add_argument("--retry-failed", action="store_true",
                     help="re-attempt files recorded as failed in a previous run")
    ap.add_argument("--version", action="version",
                     version=f"seinn-convert {CONVERT_VERSION}")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not os.path.isdir(args.path):
        print(f"seinn-convert: {args.path!r} is not a directory", file=sys.stderr)
        return 2

    root = args.path
    min_age_seconds = args.min_age * 60
    if args.force:
        print("seinn-convert: --force is lifting the recency guard — make sure "
              "nothing is actively writing into this folder", flush=True)

    entries, stale_temps = discover(root, min_age_seconds, args.force)
    if stale_temps:
        print(f"seinn-convert: {len(stale_temps)} stale temp file(s) from a "
              f"previous interrupted run found (not converted, cleanup only):")
        for t in stale_temps:
            print(f"  {t}")

    store = StateStore(root)
    probed_count = 0
    cached_count = 0
    for e in entries:
        if e.verdict is not None:
            continue  # not-video / skipped-recent already assigned by discover()
        fresh = classify_with_cache(e, store, args.retry_failed)
        if fresh:
            probed_count += 1
        else:
            cached_count += 1
    store.flush_now()

    vaapi_ok, vaapi_reason = vaapi_available()
    lane = "vaapi" if vaapi_ok else "software"
    print(f"seinn-convert: encode lane = {lane} ({vaapi_reason})", flush=True)

    remux_audio_count = sum(1 for e in entries if e.plan == "remux-audio")
    print_census(entries, root, args.min_age, probed_count, cached_count,
                 vaapi_ok, remux_audio_count)

    if not args.apply:
        return 0

    convertible = [e for e in entries if e.plan in ("remux", "remux-audio", "transcode")]
    if not convertible:
        print("\nseinn-convert: nothing to convert.")
        return 0

    stop_ctrl = StopController()
    prev_int = signal.signal(signal.SIGINT, stop_ctrl.handle_signal)
    prev_term = signal.signal(signal.SIGTERM, stop_ctrl.handle_signal)
    start = time.time()
    try:
        interrupted = apply_all(convertible, store, args.workers, vaapi_ok, stop_ctrl)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
    elapsed = time.time() - start

    had_failures = print_final_report(convertible, elapsed, vaapi_ok, vaapi_reason)

    if interrupted:
        print("seinn-convert: interrupted — state saved, re-run to resume.")
        return 130
    return 1 if had_failures else 0


if __name__ == "__main__":
    sys.exit(main())
