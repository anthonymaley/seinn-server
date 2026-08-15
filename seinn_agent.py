#!/usr/bin/env python3
"""seinn agent — HTTP media agent for self-hosted libraries.

Endpoints:
  GET  /api/roots                      -> {"roots": ["live", ...]}
  GET  /api/list?root=live&path=a/b    -> {"root", "path", "entries": [...]}
       &recursive=1 (also "true"/"yes") -> flattened subtree: files only,
       name is the path relative to `path`, btime always null, no count
  GET  /api/progress?root=&path=       -> {"position", "duration", "watched"}
  POST /api/progress  {root,path,position,duration[,watched][,reset]}
  GET  /api/session/challenge          -> {"nonce","audience","expires"}
       (Phase D — single-use nonce for a Krutho presentation, 120 s TTL)
  POST /api/session  {token}           -> {"session_token", "expires"} (bridging
       exchange: interim shared token in, short-lived HMAC session token out)
       OR  {vp_token, presentation_submission, nonce} -> same shape (Phase D:
       real Krutho credential-presentation verify via ctypes against
       libkrutho.so; 401 with a krutho_error_t name on failure). Both body
       shapes share this one endpoint; bridging keeps working either way.
  POST /api/enrol/start                -> {"enrol_id","code"} (no auth — the
       TV has nothing yet; Phase E, 5 min TTL, crude 3-outstanding cap)
  POST /api/enrol/approve {code}       -> {"approved":true} (requires a
       credential-backed session token in X-Seinn-Token — the interim
       token and bridging sessions do NOT approve)
  GET  /api/enrol/poll?enrol_id=       -> {"status":"pending"} or
       {"session_token","expires"} (device session, 30 day TTL default;
       single collection, consumed on delivery)
  GET  /api/thumb?root=live&path=a.mp4 -> JPEG poster frame (ffmpeg, disk-cached)
  GET  /files/<root>/<relpath>         -> file bytes, single-range support
  HEAD /files/<root>/<relpath>         -> headers only
  DELETE /files/<root>/<relpath>       -> 403 unless delete_enabled

Entry fields: name, is_dir, size, mtime, btime (null when unknown), type
(lowercase extension, "dir" for directories). Non-dir entries also carry
duration (seconds, null until the background sweeper has probed the file).
Timestamps are integer epoch seconds. `root` may be omitted when exactly one
root is configured. Dotfiles
(incl. AppleDouble ._* junk in this corpus) are hidden unless
hide_dotfiles = false.

Stdlib only. Linux has no os.stat st_birthtime (verified on Linux,
Python 3.13); btime comes from one batched coreutils `stat -c %W` call per
listing there. macOS has `st_birthtime` natively; the coreutils shell-out
never runs on Darwin (HAVE_BIRTHTIME gates both sides). Reads stay open —
LAN-only deployment, same posture as the spike. State-changing requests
(POST /api/progress, POST /api/session,
DELETE /files/...) require the X-Seinn-Token header: either the configured
interim shared token, or a session token minted by POST /api/session
(Phase C, stateless HMAC, stdlib hmac/hashlib/base64/secrets). With no
auth_token configured the gate is open and startup says so loudly.

Phase D adds real Krutho verification via ctypes — stdlib, no pip. The
agent must never fail to start because auth's future is missing: if
libkrutho.so can't be loaded (OSError — missing file, wrong arch, ...),
startup logs krutho=ABSENT (bridging mode only) and everything else keeps
working exactly as before.
"""

import argparse
import base64
import ctypes
import errno
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, unquote, urlparse

# bump on any behavior change; --version and the startup banner print it
AGENT_VERSION = "1.4.0"

CHUNK = 1024 * 1024
STAT_BATCH = 400
MAX_BODY = 1 << 20        # progress payloads are a few hundred bytes
HAVE_BIRTHTIME = hasattr(os.stat(__file__), "st_birthtime")
IS_DARWIN = sys.platform == "darwin"

# optional dependency — thumbs and durations degrade to absent, everything
# else must keep working (marketing promise: zero dependencies)
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_FFPROBE = shutil.which("ffprobe") is not None

# ffmpeg poster frames: try a late offset first so posters aren't black
# intros, then walk back for short files. The last entry is None = no -ss at
# all, which is the only thing that works for still images: with the image2
# demuxer even `-ss 0` consumes the single frame and yields 0 bytes (measured
# on the reference server, ffmpeg 7.1.1).
THUMB_OFFSETS = (120, 20, None)
THUMB_WIDTH = 480
THUMB_SLOTS = threading.Semaphore(3)  # ffmpeg is the expensive part, not HTTP

# Watched state. Server-side rather than per-app, so it survives a reinstall and
# so a second client (the planned iOS app) sees the same history.
WATCHED_FRACTION = 0.92   # past this, treat as finished rather than in-progress
RESUME_FLOOR = 30.0       # don't offer to resume the first few seconds

# Mirrors Agent.swift's videoTypes — one definition of "video" for the whole
# system, so the sweeper doesn't probe files the client wouldn't play.
VIDEO_EXTS = {"mp4", "mkv", "ts", "mov", "m4v", "avi", "webm"}

DUR_SWEEP_INTERVAL = 3600   # seconds between full sweeps of every root
DUR_PROBE_PAUSE = 0.05      # disks are rotational and shared with live playback

# ---- session tokens (Phase C) -----------------------------------------
#
# Stateless HMAC session tokens: no table, no cleanup thread, no server-side
# state at all beyond the secret. stdlib hmac/hashlib/base64/secrets only —
# same zero-dependency posture as the rest of the agent. This is the
# 16 ms-verify-cost countermeasure from the Krutho spike: whatever Phase D's
# credential verify costs, it's paid once at session establishment, not on
# every request — everyday requests do one cheap keyed compare instead.
#
# Token shape: payload = base64url("v1.<subject>.<expiry-epoch>"),
# sig = HMAC-SHA256(session_secret, payload), token = payload + "." + sig.
# Signature is hex (not base64url) purely so the token has exactly one "."
# to split on — base64url's alphabet has no "." in it, so this can't
# collide with a "." inside the payload half.
SESSION_TOKEN_VERSION = "v1"


def _session_payload_encode(subject, expires):
    raw = f"{SESSION_TOKEN_VERSION}.{subject}.{expires}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _session_payload_decode(payload_b64):
    # urlsafe_b64decode requires padding; we stripped it on encode, so put
    # it back rather than ship an unpadded encoder/decoder mismatch.
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    parts = raw.split(".", 2)
    if len(parts) != 3 or parts[0] != SESSION_TOKEN_VERSION:
        return None
    subject, expires_str = parts[1], parts[2]
    try:
        expires = int(expires_str)
    except ValueError:
        return None
    return subject, expires


def mint_session_token(cfg, subject, ttl=None):
    """Issue a session token for `subject`, good for `ttl` seconds from now
    (default cfg['session_ttl']; Phase E's device sessions pass
    cfg['device_session_ttl'] instead — 30 days vs. 12 h, same token shape).
    Returns (token, expires_epoch)."""
    expires = int(time.time()) + (ttl if ttl is not None else cfg["session_ttl"])
    payload = _session_payload_encode(subject, expires)
    sig = hmac.new(cfg["session_secret"], payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", expires


def verify_session_token(cfg, token):
    """Return the subject if `token` is a well-formed, correctly-signed,
    unexpired session token; None otherwise. Constant-time signature
    compare — this is an auth gate, same discipline as authorized()."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    expected_sig = hmac.new(cfg["session_secret"], payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    decoded = _session_payload_decode(payload)
    if decoded is None:
        return None
    subject, expires = decoded
    if expires < int(time.time()):
        return None
    return subject


def selftest_session():
    """Mint, validate, tamper-reject, expiry-reject a token in-process — no
    HTTP, no config file. Same doctrine as the app's --sim-* hooks: a cheap
    in-process check that doesn't need the real deployment around it."""
    cfg = {"session_secret": secrets.token_bytes(32), "session_ttl": 43200}
    ok = True

    token, expires = mint_session_token(cfg, "test-subject")
    subject = verify_session_token(cfg, token)
    if subject == "test-subject" and expires > int(time.time()):
        print("PASS: mint + validate round-trip")
    else:
        print(f"FAIL: mint + validate round-trip (got subject={subject!r})")
        ok = False

    payload, _, sig = token.rpartition(".")
    tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    tampered = f"{payload}.{tampered_sig}"
    if verify_session_token(cfg, tampered) is None:
        print("PASS: tampered signature rejected")
    else:
        print("FAIL: tampered signature accepted")
        ok = False

    expired_payload = _session_payload_encode("test-subject", int(time.time()) - 10)
    expired_sig = hmac.new(cfg["session_secret"], expired_payload.encode(),
                            hashlib.sha256).hexdigest()
    expired_token = f"{expired_payload}.{expired_sig}"
    if verify_session_token(cfg, expired_token) is None:
        print("PASS: expired token rejected")
    else:
        print("FAIL: expired token accepted")
        ok = False

    print("SELFTEST-SESSION: " + ("PASS" if ok else "FAIL"))
    return ok


# ---- Krutho credential verify via ctypes (Phase D) ---------------------
#
# ctypes is stdlib — no pip, no sidecar. libkrutho.so is a Linux x86_64
# build (CMake out-of-tree over ~/krutho-new/packages/{verifier-sdk,oid4vp},
# -DCMAKE_POSITION_INDEPENDENT_CODE=ON, linked into one .so exporting the
# five verifier_sdk.h functions — recipe in
# docs/research/2026-07-30-krutho-auth-spike.md "Spike A"). It is a build
# artifact of another repo, deployed to /opt/seinn/libkrutho.so beside this
# script, never committed here. This Mac has no build of it (Linux x86_64
# only) — --selftest-krutho on the Mac always hits the SKIP path below, and
# that skip IS the correct Mac-side behavior, not a test gap.
#
# Struct layout and function signatures mirror verifier_sdk.h exactly (and
# the spike's proven ctypes binding — 10/10 assertions, median 15.9 ms).

KRUTHO_OK = 0
KRUTHO_ERROR_NAMES = {
    0: "KRUTHO_OK",
    1: "INVALID_PARAM",
    2: "REVOKED",
    3: "INVALID_NONCE",
    4: "INVALID_AUDIENCE",
    5: "KB_JWT_SIGNATURE",
    6: "X5C_CHAIN",
    7: "ISSUER_SIGNATURE",
    8: "DISCLOSURE_INTEGRITY",
    9: "PARSE",
}


class _KruthoVerifyResult(ctypes.Structure):
    _fields_ = [("claims_json", ctypes.c_char_p)]


class _KruthoAuthRequest(ctypes.Structure):
    _fields_ = [("json", ctypes.c_char_p)]


def load_krutho(lib_path):
    """Load libkrutho.so and declare ctypes signatures for all five
    verifier_sdk.h functions. Returns the CDLL, or None on OSError (missing
    file, wrong arch, ...) — caller logs krutho=ABSENT and moves on. The
    agent must never fail to start because auth's future is missing."""
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError:
        return None

    lib.krutho_verify.restype = ctypes.c_int
    lib.krutho_verify.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t,          # vp_token, len
        ctypes.c_char_p, ctypes.c_size_t,          # presentation_submission, len
        ctypes.c_char_p, ctypes.c_size_t,          # trust_chain, len
        ctypes.c_char_p, ctypes.c_size_t,          # revocation_list, len
        ctypes.c_char_p,                           # expected_nonce
        ctypes.c_char_p,                           # expected_audience
        ctypes.POINTER(_KruthoVerifyResult),
    ]
    lib.krutho_verify_result_free.restype = None
    lib.krutho_verify_result_free.argtypes = [ctypes.POINTER(_KruthoVerifyResult)]

    lib.krutho_check_revoked.restype = ctypes.c_int
    lib.krutho_check_revoked.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]

    # Declared for completeness (mirrors the spike's binding) — not called
    # by the /api/session flow today; the agent verifies presentations, it
    # doesn't build authorization requests yet.
    lib.krutho_create_authorization_request.restype = ctypes.c_int
    lib.krutho_auth_request_free.restype = None
    lib.krutho_auth_request_free.argtypes = [ctypes.POINTER(_KruthoAuthRequest)]

    return lib


def krutho_verify(lib, vp_token, presentation_submission, trust_chain,
                   revocation_list, nonce, audience):
    """One verify call. Returns (error_code, claims_dict_or_None).

    Memory contract: every call is paired with krutho_verify_result_free
    here, before returning — the caller-allocated struct never leaks past
    this function.
    """
    result = _KruthoVerifyResult()
    rc = lib.krutho_verify(
        vp_token.encode(), len(vp_token.encode()),
        presentation_submission.encode(), len(presentation_submission.encode()),
        trust_chain, len(trust_chain),
        revocation_list, len(revocation_list),
        nonce.encode(), audience.encode(),
        ctypes.byref(result))
    claims = None
    if result.claims_json:
        try:
            claims = json.loads(result.claims_json)
        except ValueError:
            claims = None
    lib.krutho_verify_result_free(ctypes.byref(result))
    return rc, claims


def selftest_krutho(lib_path, fixture_dir):
    """One fixture verify + the negative controls, in-process. Skips with a
    clear message when the lib is absent (default path, Mac side) — that
    skip is the correct Mac behavior, not a failure. fixture_dir is the SDK
    test corpus root (contains oid4vp/c/test/test_data and
    verifier-sdk/test_data) — only present on hosts carrying the Krutho
    SDK fixtures."""
    lib = load_krutho(lib_path)
    if lib is None:
        print(f"SKIP: {lib_path} absent — libkrutho.so has no macOS build "
              f"(Linux x86_64 only); this IS the Mac-side behavior")
        return True

    oid4vp_data = os.path.join(fixture_dir, "packages/oid4vp/c/test/test_data")
    verifier_data = os.path.join(fixture_dir, "packages/verifier-sdk/test_data")

    def read_trim(path):
        with open(path, "rb") as f:
            return f.read().rstrip(b"\r\n \t").decode()

    def read_bin(path):
        with open(path, "rb") as f:
            return f.read()

    try:
        vp = read_trim(os.path.join(oid4vp_data, "valid_presentation.txt"))
        ps = read_trim(os.path.join(oid4vp_data, "presentation_submission.json"))
        tc = read_bin(os.path.join(oid4vp_data, "trust_chain.der"))
        nonce = read_trim(os.path.join(oid4vp_data, "expected_nonce.txt"))
        aud = read_trim(os.path.join(oid4vp_data, "expected_audience.txt"))
        rl_clean = read_bin(os.path.join(verifier_data, "test_revocation_filter"))
        rl_revoked = read_bin(os.path.join(verifier_data, "test_revocation_filter_verify"))
        tampered_vp = read_trim(os.path.join(oid4vp_data, "tampered_issuer_presentation.txt"))
    except OSError as exc:
        print(f"FAIL: --selftest-krutho fixtures missing under {fixture_dir}: {exc}")
        return False

    ok = True

    rc, claims = krutho_verify(lib, vp, ps, tc, rl_clean, nonce, aud)
    if rc == KRUTHO_OK and claims:
        print(f"PASS: valid presentation -> KRUTHO_OK, claims={claims}")
    else:
        print(f"FAIL: valid presentation -> {KRUTHO_ERROR_NAMES.get(rc, rc)}")
        ok = False

    rc, _ = krutho_verify(lib, vp, ps, tc, rl_revoked, nonce, aud)
    if rc == 2:
        print("PASS: revoked credential -> REVOKED")
    else:
        print(f"FAIL: revoked credential -> {KRUTHO_ERROR_NAMES.get(rc, rc)}")
        ok = False

    rc, _ = krutho_verify(lib, vp, ps, tc, rl_clean, "wrong-nonce", aud)
    if rc == 3:
        print("PASS: wrong nonce -> INVALID_NONCE")
    else:
        print(f"FAIL: wrong nonce -> {KRUTHO_ERROR_NAMES.get(rc, rc)}")
        ok = False

    rc, _ = krutho_verify(lib, vp, ps, tc, rl_clean, nonce, "wrong-audience")
    if rc == 4:
        print("PASS: wrong audience -> INVALID_AUDIENCE")
    else:
        print(f"FAIL: wrong audience -> {KRUTHO_ERROR_NAMES.get(rc, rc)}")
        ok = False

    rc, _ = krutho_verify(lib, tampered_vp, ps, tc, rl_clean, nonce, aud)
    if rc == 7:
        print("PASS: tampered issuer JWT -> ISSUER_SIGNATURE")
    else:
        print(f"FAIL: tampered issuer JWT -> {KRUTHO_ERROR_NAMES.get(rc, rc)}")
        ok = False

    print("SELFTEST-KRUTHO: " + ("PASS" if ok else "FAIL"))
    return ok


# ---- enrolment (Phase E) -----------------------------------------------
#
# TV endorsement flow: the TV has no camera, no biometrics, and holds no
# credential or key material, ever (comment states the model, per the
# spec's design). It requests a short code from /api/enrol/start, a
# credential-holding phone approves that code via /api/enrol/approve, and
# the TV polls /api/enrol/poll until it gets back a device session token —
# the same stateless HMAC shape as every other session token, just with a
# much longer TTL (30 days default) and a subject of the form
# "device:<enrol_id-prefix>".
#
# State is one in-memory dict, purged opportunistically (no cleanup
# thread — same doctrine as the nonce store). Losing in-flight enrolments
# on a restart is acceptable: these are 5-minute windows, and a lost
# pairing just gets retried (Phase E risk #3 in the spec).
#
# approve() takes an already-resolved subject string, not a raw token —
# the caller (Handler.credential_backed_subject) is what enforces that the
# approving session is credential-backed (subject starts with
# "credential:") and not the interim shared token or a bridging session.
# That refusal is deliberate: the interim token does NOT approve
# enrolments, because endorsement (a real credential vouching for a new
# device) is the whole security model this phase adds. The only escape
# hatch is --test-treat-bridging-as-credential, a DEBUG-only flag that
# lets a bridging session ("bridge") count as credential-backed so the
# enrolment flow is exercisable on this Mac without libkrutho.so; never
# set it in production config.

ENROL_TTL_DEFAULT = 300
DEVICE_SESSION_TTL_DEFAULT = 30 * 86400
MAX_OUTSTANDING_ENROL = 3


def enrol_purge(cfg, now):
    """Caller must hold cfg['enrol_lock']. Drops expired and
    already-delivered entries."""
    dead = [eid for eid, e in cfg["enrol"].items()
            if e["expires"] < now or e["delivered"]]
    for eid in dead:
        del cfg["enrol"][eid]


def enrol_start(cfg):
    """Returns (enrol_id, code), or None if the crude outstanding-enrolment
    cap is hit (LAN posture, comment states it's not a real rate limiter —
    just enough to stop a runaway loop from filling the dict)."""
    now = int(time.time())
    with cfg["enrol_lock"]:
        enrol_purge(cfg, now)
        outstanding = sum(1 for e in cfg["enrol"].values() if not e["approved"])
        if outstanding >= MAX_OUTSTANDING_ENROL:
            return None
        enrol_id = secrets.token_urlsafe(16)
        code = f"{secrets.randbelow(1000000):06d}"
        cfg["enrol"][enrol_id] = {
            "code": code, "expires": now + cfg["enrol_ttl"],
            "approved": False, "approved_subject": None,
            "session_token": None, "session_expires": None,
            "delivered": False,
        }
        return enrol_id, code


def enrol_approve(cfg, code, approving_subject):
    """Marks the outstanding enrolment matching `code` approved and mints
    its device session token now (delivered later via poll, once — single
    collection). Returns True if a live, unapproved match was found."""
    now = int(time.time())
    with cfg["enrol_lock"]:
        enrol_purge(cfg, now)
        match = None
        for eid, e in cfg["enrol"].items():
            if e["code"] == code and not e["approved"]:
                match = eid
                break
        if match is None:
            return False
        entry = cfg["enrol"][match]
        entry["approved"] = True
        entry["approved_subject"] = approving_subject
        token, expires = mint_session_token(
            cfg, f"device:{match[:8]}", ttl=cfg["device_session_ttl"])
        entry["session_token"] = token
        entry["session_expires"] = expires
        return True


def enrol_poll(cfg, enrol_id):
    """Returns one of:
      ("unknown", None)        — no such id, or it expired
      ("pending", None)        — outstanding, not yet approved
      ("ok", (token, expires)) — approved; this call consumes the entry
    """
    now = int(time.time())
    with cfg["enrol_lock"]:
        entry = cfg["enrol"].get(enrol_id)
        if entry is None or entry["expires"] < now:
            return "unknown", None
        if not entry["approved"]:
            return "pending", None
        token, expires = entry["session_token"], entry["session_expires"]
        entry["delivered"] = True
        del cfg["enrol"][enrol_id]
        return "ok", (token, expires)


def selftest_enrol():
    """start -> approve (credential-backed subject) -> poll happy path;
    a non-credential subject string never reaches approve() from the real
    Handler path (credential_backed_subject() gates that, one layer up —
    this test exercises enrol_*() directly, so it checks the string
    invariant those functions rely on); single-collection consumption;
    expiry. In-process, no HTTP — same doctrine as selftest_session and
    selftest_krutho."""
    cfg = {"session_secret": secrets.token_bytes(32), "session_ttl": 43200,
           "device_session_ttl": 2592000, "enrol_ttl": 300,
           "enrol": {}, "enrol_lock": threading.Lock()}
    ok = True

    got = enrol_start(cfg)
    if got is None:
        print("FAIL: enrol_start returned None on first call")
        return False
    enrol_id, code = got
    print(f"PASS: enrol_start -> enrol_id={enrol_id[:8]}... code={code}")

    if "bridge".startswith("credential:"):
        print("FAIL: sanity check on the credential: prefix itself")
        ok = False
    else:
        print("PASS: 'bridge' subject does not carry the credential: prefix "
              "(the real rejection path — credential_backed_subject() — is "
              "HTTP-header-shaped and covered by the curl script, not here)")

    if enrol_approve(cfg, code, "credential:SM-123456"):
        print("PASS: approve with correct code")
    else:
        print("FAIL: approve with correct code")
        ok = False

    status, payload = enrol_poll(cfg, enrol_id)
    if status == "ok" and payload and payload[0]:
        subject = verify_session_token(cfg, payload[0])
        if subject == f"device:{enrol_id[:8]}":
            print(f"PASS: poll -> device session, subject={subject}")
        else:
            print(f"FAIL: poll session token subject mismatch: {subject!r}")
            ok = False
    else:
        print(f"FAIL: poll -> {status}")
        ok = False

    status2, _ = enrol_poll(cfg, enrol_id)
    if status2 == "unknown":
        print("PASS: second poll -> unknown (single collection, consumed)")
    else:
        print(f"FAIL: second poll -> {status2} (expected unknown/consumed)")
        ok = False

    cfg["enrol_ttl"] = 0
    enrol_id2, code2 = enrol_start(cfg)
    time.sleep(1.1)
    if not enrol_approve(cfg, code2, "credential:SM-123456"):
        print("PASS: expired enrolment not approvable")
    else:
        print("FAIL: expired enrolment was approved")
        ok = False

    print("SELFTEST-ENROL: " + ("PASS" if ok else "FAIL"))
    return ok


def db_connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS progress (
                        root     TEXT NOT NULL,
                        path     TEXT NOT NULL,
                        position REAL NOT NULL DEFAULT 0,
                        duration REAL NOT NULL DEFAULT 0,
                        watched  INTEGER NOT NULL DEFAULT 0,
                        updated  INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (root, path))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS durations (
                        root     TEXT NOT NULL,
                        path     TEXT NOT NULL,
                        mtime    INTEGER NOT NULL,
                        duration REAL,
                        PRIMARY KEY (root, path))""")
    conn.commit()
    return conn


def probe_duration(full):
    """ffprobe duration in seconds, or None if the probe fails or the output
    doesn't parse. Caller still records the row (with the current mtime) on
    None, so a bad file isn't re-probed every sweep."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", full],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


def duration_sweep_loop(cfg):
    """Keeps the durations table current so listings can carry per-file
    duration without a per-request ffprobe. Runs forever on a daemon thread;
    a full sweep of every root, then sleep, repeat. Dot-directories are
    pruned from descent to match send_recursive_list's dot-file filter.
    """
    if not HAVE_FFPROBE:
        # Do not record any rows here: a (mtime, NULL) row per file would
        # otherwise block backfill until each file's mtime changes, even
        # after ffprobe is installed later.
        print("DUR sweep disabled: ffprobe not found — durations will be null",
              flush=True)
        return
    hide = cfg["hide_dotfiles"]
    while True:
        for name, base in cfg["roots"].items():
            probed = total = 0
            try:
                conn = db_connect(cfg["state_db"])
            except sqlite3.Error:
                continue
            for cur, dirs, files in os.walk(base):
                if hide:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    ext = os.path.splitext(fname)[1].lstrip(".").lower()
                    if ext not in VIDEO_EXTS:
                        continue
                    total += 1
                    full = os.path.join(cur, fname)
                    try:
                        mtime = int(os.stat(full).st_mtime)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, base)
                    row = conn.execute(
                        "SELECT mtime FROM durations WHERE root = ? AND path = ?",
                        (name, rel)).fetchone()
                    if row is not None and row[0] == mtime:
                        continue
                    duration = probe_duration(full)
                    conn.execute(
                        "INSERT INTO durations (root, path, mtime, duration) "
                        "VALUES (?,?,?,?) ON CONFLICT(root, path) DO UPDATE SET "
                        "mtime=excluded.mtime, duration=excluded.duration",
                        (name, rel, mtime, duration))
                    conn.commit()
                    probed += 1
                    time.sleep(DUR_PROBE_PAUSE)
            conn.close()
            print(f"DUR sweep root={name} probed={probed} total={total}", flush=True)
        time.sleep(DUR_SWEEP_INTERVAL)


def load_config(argv):
    ap = argparse.ArgumentParser(description="seinn media agent")
    ap.add_argument("--config", help="TOML config path")
    ap.add_argument("--port", type=int)
    ap.add_argument("--bind")
    ap.add_argument("--root", action="append", default=[],
                    help="name=path (repeatable; overrides config roots)")
    ap.add_argument("--version", action="version",
                    version=f"seinn-agent {AGENT_VERSION}")
    ap.add_argument("--selftest-session", action="store_true",
                    help="mint/validate/tamper-reject/expiry-reject a session "
                         "token in-process and exit 0/1 (no HTTP, no config "
                         "needed) — hidden flag, not part of normal operation")
    ap.add_argument("--selftest-krutho", action="store_true",
                    help="load libkrutho.so and run one fixture verify + "
                         "negative controls in-process; SKIPs cleanly when "
                         "the lib is absent (e.g. on the Mac) — hidden flag")
    ap.add_argument("--selftest-enrol", action="store_true",
                    help="start->approve->poll happy path + single-collection "
                         "+ expiry, in-process, no HTTP — hidden flag")
    ap.add_argument("--doctor", action="store_true",
                    help="read-only diagnosis of a deployed install: nine "
                         "checks, one line each, PASS/WARN/FAIL/SKIP/INFO, "
                         "ending in a DOCTOR: PASS/ISSUES verdict — never "
                         "writes, never restarts, safe against a live server")
    ap.add_argument("--service-name", default="seinn-agent",
                    help="systemd unit name for --doctor's service checks "
                         "(default seinn-agent)")
    ap.add_argument("--test-treat-bridging-as-credential", action="store_true",
                    help="DEBUG-ONLY: let a bridging session (subject == "
                         "'bridge') pass credential_backed_subject(), so "
                         "/api/enrol/approve is exercisable on this Mac "
                         "without libkrutho.so. Never set this in production "
                         "config — it defeats the point of Phase E (that "
                         "enrolment approval requires a real credential).")
    ap.add_argument("--krutho-fixture-dir", default=os.path.expanduser("~/krutho-new"),
                    help="SDK tree root for --selftest-krutho's fixtures "
                         "(default ~/krutho-new)")
    ap.add_argument("--test-accept-nonce", default=None,
                    help="TEST-ONLY: pre-seed the outstanding-nonce set with "
                         "this exact value at startup, as if it had just been "
                         "issued by GET /api/session/challenge. Exists because "
                         "the SDK's fixture presentation has a nonce baked "
                         "into its signed KB-JWT (it can't be rebound without "
                         "holder key material we don't have) — this lets the "
                         "fixture verify exercise the real single-use-nonce "
                         "path in D3 instead of skipping it. Never set this "
                         "in production config.")
    ap.add_argument("--test-accept-audience", default=None,
                    help="TEST-ONLY: override the expected audience "
                         "(normally the constant 'seinn-agent') for this "
                         "process only. Same reason as --test-accept-nonce: "
                         "the SDK's fixture presentation's KB-JWT was signed "
                         "for the SDK's own test audience ('partner-verifier'), "
                         "not 'seinn-agent', and can't be rebound without "
                         "holder key material we don't have. Never set this "
                         "in production config.")
    args = ap.parse_args(argv)

    if args.selftest_session:
        # Runs before any config/roots validation below — the self-test is
        # deliberately independent of a real deployment.
        sys.exit(0 if selftest_session() else 1)

    if args.selftest_krutho:
        default_lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libkrutho.so")
        sys.exit(0 if selftest_krutho(default_lib, args.krutho_fixture_dir) else 1)

    if args.selftest_enrol:
        sys.exit(0 if selftest_enrol() else 1)

    if args.doctor:
        # Dispatched before any of the hard config validation below, same
        # doctrine as the --selftest-* flags above: a broken config must be
        # a *finding* doctor reports, never a crash that stops it running.
        # --config defaults to the install layout's path here (not None)
        # so a custom-prefix user sees exactly which path doctor looked at.
        config_path = args.config or doctor_default_config_path()
        sys.exit(run_doctor(config_path, args.service_name))

    cfg = {"port": 8378, "bind": "0.0.0.0", "delete_enabled": False,
           "hide_dotfiles": True, "thumbs_enabled": True,
           "cache_dir": "/var/tmp/seinn-thumbs", "auth_token": "",
           "state_db": "/opt/seinn/progress.db", "roots": {},
           # session_secret unset -> generated per-process below (restart
           # invalidates outstanding sessions; clients silently re-establish
           # via /api/session). Configure it in the TOML to survive restarts.
           "session_secret": "", "session_ttl": 43200,
           # Phase D: krutho_lib defaults to a .so next to this script.
           # krutho_trust_chain / krutho_revocation_list are file paths,
           # bytes loaded once at startup (reloaded on restart only) —
           # sourced from the SDK's test fixtures for now ([issuer-gated]:
           # real trust chains and live CRLite filters need Krutho issuer
           # infrastructure that does not exist here).
           "krutho_lib": os.path.join(os.path.dirname(os.path.abspath(__file__)), "libkrutho.so"),
           "krutho_trust_chain": "", "krutho_revocation_list": "",
           # Phase E: enrolment TTL (5 min, code outstanding) and device
           # session TTL (30 days — the TV, unlike the phone, isn't
           # re-presenting a credential every 12h; endorsement happened
           # once at pairing time).
           "enrol_ttl": ENROL_TTL_DEFAULT,
           "device_session_ttl": DEVICE_SESSION_TTL_DEFAULT}
    cfg["test_accept_nonce"] = args.test_accept_nonce
    cfg["krutho_audience"] = args.test_accept_audience or "seinn-agent"
    cfg["test_treat_bridging_as_credential"] = args.test_treat_bridging_as_credential
    if args.config:
        try:
            with open(args.config, "rb") as f:
                loaded = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            ap.error(f"config {args.config}: {exc}")
        cfg.update({k: v for k, v in loaded.items() if k != "roots"})
        cfg["roots"].update(loaded.get("roots", {}))
    for spec in args.root:
        name, _, path = spec.partition("=")
        if not path:
            ap.error(f"--root needs name=path, got {spec!r}")
        cfg["roots"][name] = path
    if args.port:
        cfg["port"] = args.port
    if args.bind:
        cfg["bind"] = args.bind
    if not cfg["roots"]:
        ap.error("no roots configured (config [roots] or --root name=path)")

    # A stray top-level key placed below [roots] in the TOML lands in this
    # dict as a non-path value (2026-07-30, twice) and would otherwise be
    # silently served as a public share. Catching a non-absolute-path value
    # here turns that silent token leak into a refusal to start. Limitation:
    # a leaked value that happens to look like an absolute path still passes.
    for name, value in cfg["roots"].items():
        if not isinstance(value, str) or not os.path.isabs(value):
            ap.error(f"root {name!r} = {value!r} is not an absolute path — if this is a "
                     f"setting like auth_token, it must sit ABOVE [roots] in the config")

    cfg["roots"] = {n: os.path.realpath(p) for n, p in cfg["roots"].items()}
    for name, path in cfg["roots"].items():
        if not os.path.isdir(path):
            print(f"WARNING: root {name} -> {path} is not a directory "
                  f"(missing mount? typo?)", flush=True)
        elif not os.access(path, os.R_OK | os.X_OK):
            print(f"WARNING: root {name} -> {path} is not readable by user "
                  f"{getpass.getuser()}", flush=True)

    # session_secret: configured value (a TOML string) survives restarts;
    # unset means a fresh secrets.token_bytes(32) every process start, so a
    # restart invalidates every outstanding session token (comment states
    # this trade — clients hit 401 and silently re-establish via
    # /api/session). Same must-sit-above-[roots] trap as every other
    # top-level key (that trap has bitten twice already for auth_token).
    if cfg["session_secret"]:
        cfg["session_secret"] = str(cfg["session_secret"]).encode()
    else:
        cfg["session_secret"] = secrets.token_bytes(32)
    cfg["session_ttl"] = int(cfg["session_ttl"])
    cfg["enrol_ttl"] = int(cfg["enrol_ttl"])
    cfg["device_session_ttl"] = int(cfg["device_session_ttl"])

    # Phase D: load libkrutho.so once at startup. OSError (missing file,
    # wrong arch — e.g. a dev Mac with only a Linux x86_64 build around)
    # means krutho=ABSENT and the agent runs in bridging-only mode; it must
    # never fail to start because auth's future is missing.
    cfg["krutho"] = load_krutho(cfg["krutho_lib"])
    cfg["krutho_trust_chain_bytes"] = b""
    cfg["krutho_revocation_list_bytes"] = b""
    if cfg["krutho"] is not None:
        try:
            if cfg["krutho_trust_chain"]:
                with open(cfg["krutho_trust_chain"], "rb") as f:
                    cfg["krutho_trust_chain_bytes"] = f.read()
            if cfg["krutho_revocation_list"]:
                with open(cfg["krutho_revocation_list"], "rb") as f:
                    cfg["krutho_revocation_list_bytes"] = f.read()
        except OSError as exc:
            print(f"WARNING: krutho trust material unreadable ({exc}) — "
                  f"credential verify will fail until this is fixed; "
                  f"bridging mode is unaffected", flush=True)

    # In-memory single-use nonce store for the challenge/verify flow: a
    # small dict, purged opportunistically on each issue (stdlib, no
    # cleanup thread — same doctrine as the enrolment state in Phase E).
    # A lock because ThreadingHTTPServer runs handlers on separate threads
    # all sharing this one cfg dict.
    cfg["nonces"] = {}
    cfg["nonces_lock"] = threading.Lock()

    # Phase E: in-memory enrolment state, same doctrine — one dict, one
    # lock, purged opportunistically, no cleanup thread.
    cfg["enrol"] = {}
    cfg["enrol_lock"] = threading.Lock()
    if cfg["test_treat_bridging_as_credential"]:
        print("WARNING: --test-treat-bridging-as-credential is set — "
              "TEST-ONLY, do not run this in production (it defeats Phase "
              "E's whole point: enrolment approval requiring a real "
              "credential)", flush=True)
    if cfg["test_accept_nonce"]:
        # TEST-ONLY: see --test-accept-nonce help text above. Pre-seeding
        # here reuses the exact same single-use consumption path a real
        # challenge-issued nonce goes through — no separate code path to
        # trust.
        with cfg["nonces_lock"]:
            cfg["nonces"][cfg["test_accept_nonce"]] = int(time.time()) + 3600
        print(f"WARNING: --test-accept-nonce is set ({cfg['test_accept_nonce']!r}) "
              f"— TEST-ONLY, do not run this in production", flush=True)
    if args.test_accept_audience:
        print(f"WARNING: --test-accept-audience is set ({cfg['krutho_audience']!r}) "
              f"— TEST-ONLY, do not run this in production", flush=True)

    return cfg


# ---- doctor (read-only install diagnosis) ------------------------------
#
# Every probe here is read-only by construction: open()/scandir()/os.access
# for filesystem checks, socket.bind() closed in a finally, sqlite3 opened
# "mode=ro" (never a plain connect, which would create the file), systemctl
# is-active, shutil.which. No pip, no writes, no service restarts — this
# must stay safe to run against a live production server (README tells
# strangers to run it as their first debugging move).


def _doctor_fix_unreadable(path, user):
    # setfacl is the precise fix; chgrp/chmod is the fallback when it's not
    # installed. <group> stays a literal placeholder — doctor has no
    # opinion on which group to use, unlike <user> which it knows for sure.
    if shutil.which("setfacl"):
        base = f"sudo setfacl -R -m u:{user}:rX {path}"
    else:
        base = f"sudo chgrp -R <group> {path} && sudo chmod -R g+rX {path}"
    return f"{base}, or run the agent as a user that can already read it"


def _doctor_fix_unwritable(path, user):
    if shutil.which("setfacl"):
        base = f"sudo setfacl -R -m u:{user}:rwX {path}"
    else:
        base = f"sudo chgrp -R <group> {path} && sudo chmod -R g+rwX {path}"
    return f"{base}, or set delete_enabled = false to run read-only"


def _doctor_probe_readable(path):
    """os.access is a first pass only — it lies under ACLs (playbook,
    root-owned top dirs fail with a bare Permission denied). The real
    scandir()/open() below is the source of truth. Returns
    (ok, probe_file_or_None, error_or_None); error is set only when a real
    syscall (not os.access) is what failed, so callers can tell the two
    apart in the reported reason."""
    access_ok = os.access(path, os.R_OK | os.X_OK)
    try:
        probe_file = None
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_file():
                    probe_file = entry.path
                    break
    except OSError as exc:
        # The directory itself couldn't be listed — as real a failure as an
        # open() would be, and the one a chmod-000 root actually produces.
        return False, None, exc
    if probe_file is None:
        # Nothing inside to open — no real probe possible, access() is all
        # there is to go on.
        return access_ok, None, None
    try:
        with open(probe_file, "rb"):
            pass
    except OSError as exc:
        return False, probe_file, exc
    return True, probe_file, None


def _doctor_probe_writable(path, probe_file):
    """Directory-unlink permission is checked with os.access only (a real
    unlink probe would mutate; the wizard does the authoritative check at
    install time). The real open(..., "r+b") on one file proves file-level
    write ACLs without writing a byte. Returns (ok, error_or_None)."""
    access_ok = os.access(path, os.W_OK)
    open_err = None
    if probe_file is not None:
        try:
            with open(probe_file, "r+b"):
                pass
        except OSError as exc:
            open_err = exc
    return (access_ok and open_err is None), open_err


def launchd_label(service_name):
    """The Python twin of install.sh's LABEL transform — the two must agree
    so --doctor and the installer always point at the same daemon."""
    return "com.seinn." + service_name.removeprefix("seinn-")


def _doctor_systemd_active(service_name):
    if shutil.which("systemctl") is None:
        return False
    try:
        res = subprocess.run(["systemctl", "is-active", service_name],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.stdout.strip() == "active"


def _doctor_launchd_active(service_name):
    label = launchd_label(service_name)
    try:
        res = subprocess.run(["launchctl", "print", f"system/{label}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and "state = running" in res.stdout


def _doctor_service_active(service_name):
    """Platform dispatcher: Linux checks systemd, Darwin checks launchd."""
    if IS_DARWIN:
        return _doctor_launchd_active(service_name)
    return _doctor_systemd_active(service_name)


def _doctor_port_holder(port):
    """Identify who holds a busy port via ss (Linux) or lsof (Darwin). On
    this Mac ss is absent — that fallback ("holder unknown") IS the correct
    Mac-side behavior, not a test gap."""
    if IS_DARWIN:
        if shutil.which("lsof") is None:
            return "holder unknown (lsof not found)"
        try:
            res = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "holder unknown (lsof not found)"
        out = res.stdout.strip()
        return f"holder: {out}" if out else "holder unknown (lsof returned nothing)"
    if shutil.which("ss") is None:
        return "holder unknown (ss not found)"
    try:
        res = subprocess.run(["ss", "-ltnp", f"sport = :{port}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "holder unknown (ss not found)"
    out = res.stdout.strip()
    return f"holder: {out}" if out else "holder unknown (ss returned nothing)"


class CheckResult(NamedTuple):
    level: str          # "PASS" | "FAIL" | "WARN" | "INFO" | "SKIP"
    message: str        # everything before the fix, exactly as printed today
    fix: str | None     # the suffix after " — fix: ", or None


def doctor_default_config_path():
    return "/opt/seinn/seinn-agent.toml"   # single source for the --doctor default


def doctor_checks(config_path, service_name):
    """Read-only diagnosis of a deployed install. Yields one CheckResult per
    check, in today's exact order. Checks that depend on an earlier FAIL
    report SKIP with the reason instead of crashing — a broken config is a
    finding here, never an exception. Read-only by construction — same
    contract as run_doctor, which is now a printer over this generator."""
    user = getpass.getuser()

    def line(level, msg):
        if " — fix: " in msg:
            message, fix = msg.rsplit(" — fix: ", 1)
        else:
            message, fix = msg, None
        return CheckResult(level, message, fix)

    skip_reason = "config invalid (see check 1)"

    # ---- 1. config parse -----------------------------------------------
    try:
        with open(config_path, "rb") as f:
            loaded = tomllib.load(f)
    except FileNotFoundError:
        yield line("FAIL", f"config: {config_path} not found")
        loaded = None
    except OSError as exc:
        yield line("FAIL", f"config: {config_path}: {exc}")
        loaded = None
    except tomllib.TOMLDecodeError as exc:
        yield line("FAIL", f"config: {config_path}: {exc}")
        loaded = None

    doctor_cfg = None
    if loaded is not None:
        # Mirrors load_config's refusal: a stray top-level key placed below
        # [roots] in the TOML lands inside the roots table as a non-path
        # value and would otherwise be silently served as a public share.
        trap_key = None
        for key, value in loaded.get("roots", {}).items():
            if not isinstance(value, str) or not os.path.isabs(value):
                trap_key = key
                break
        if trap_key is not None:
            yield line("FAIL", f"config: {config_path}: root {trap_key!r} = "
                          f"{loaded['roots'][trap_key]!r} is not an absolute "
                          f"path — move this key ABOVE [roots] in the config")
        else:
            doctor_cfg = {
                "port": 8378, "bind": "0.0.0.0", "delete_enabled": False,
                "auth_token": "", "state_db": "/opt/seinn/progress.db",
                "krutho_lib": os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "libkrutho.so"),
            }
            doctor_cfg.update({k: v for k, v in loaded.items() if k != "roots"})
            doctor_cfg["roots"] = {n: os.path.realpath(p)
                                   for n, p in loaded.get("roots", {}).items()}
            n_roots = len(doctor_cfg["roots"])
            delete_word = "on" if doctor_cfg["delete_enabled"] else "off"
            yield line("PASS", f"config: parsed {config_path} "
                          f"({n_roots} root{'s' if n_roots != 1 else ''}, "
                          f"delete {delete_word})")

    # ---- 2. each root ---------------------------------------------------
    if doctor_cfg is None:
        yield line("SKIP", f"roots: {skip_reason}")
    else:
        for name, path in doctor_cfg["roots"].items():
            if not os.path.isdir(path):
                yield line("FAIL", f"root {name}: {path} does not exist "
                              f"(missing mount? typo?)")
                continue

            readable, probe_file, err = _doctor_probe_readable(path)
            if not readable:
                reason = (f"real probe failed: {type(err).__name__}: {err}"
                          if err is not None else "os.access denies read/exec")
                fix = _doctor_fix_unreadable(path, user)
                yield line("FAIL", f"root {name}: {path} not readable by user "
                              f"{user} ({reason}) — fix: {fix}")
                if doctor_cfg["delete_enabled"]:
                    yield line("SKIP", f"root {name} writable: root not readable")
                continue

            extra = "" if probe_file else " (empty directory, no file to open())"
            yield line("PASS", f"root {name}: readable by user {user}{extra}")

            if doctor_cfg["delete_enabled"]:
                writable, werr = _doctor_probe_writable(path, probe_file)
                if writable:
                    yield line("PASS", f"root {name} writable: writable by user "
                                  f"{user} (file write verified via real open "
                                  f"r+b; directory-unlink permission checked "
                                  f"via os.access only — a real unlink probe "
                                  f"would mutate; the wizard does the "
                                  f"authoritative check at install time)")
                else:
                    reason = (f"real probe failed: {type(werr).__name__}: {werr}"
                              if werr is not None else "os.access denies write")
                    fix = _doctor_fix_unwritable(path, user)
                    yield line("FAIL", f"root {name} writable: {path} not writable "
                                  f"by user {user} ({reason}) — fix: {fix}")

    # ---- 3. port bindable ------------------------------------------------
    if doctor_cfg is None:
        yield line("SKIP", f"port: {skip_reason}")
    else:
        bind_addr, port = doctor_cfg["bind"], doctor_cfg["port"]
        active = _doctor_service_active(service_name)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((bind_addr, port))
            yield line("PASS", f"port: {port} bindable on {bind_addr}")
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                if active:
                    yield line("PASS", f"port {port} held by the running "
                                  f"{service_name} (expected on a live "
                                  f"install)")
                else:
                    holder = _doctor_port_holder(port)
                    yield line("FAIL", f"port: {port} already in use — {holder}")
            else:
                yield line("FAIL", f"port: {port} bind failed: {exc}")
        finally:
            s.close()

    # ---- 4. ffmpeg / ffprobe ---------------------------------------------
    ffmpeg_fix = "brew install ffmpeg" if IS_DARWIN else "sudo apt install ffmpeg"
    if HAVE_FFMPEG:
        yield line("PASS", "ffmpeg: found")
    else:
        yield line("WARN", f"ffmpeg: not found — fix: {ffmpeg_fix} "
                      "(thumbnails and durations off until then)")
    if HAVE_FFPROBE:
        yield line("PASS", "ffprobe: found")
    else:
        yield line("WARN", f"ffprobe: not found — fix: {ffmpeg_fix} "
                      "(durations off until then)")

    # ---- 5. /dev/dri (VAAPI) ---------------------------------------------
    if IS_DARWIN:
        yield line("SKIP", "hardware encode: none on macOS (VideoToolbox is "
                      "a future lane); seinn-convert uses software x264")
    else:
        dri = "/dev/dri"
        if not os.path.isdir(dri):
            yield line("INFO", f"{dri}: absent — hardware encoding unavailable "
                          f"(only matters for seinn-convert, not playback)")
        else:
            try:
                render_nodes = [n for n in os.listdir(dri) if n.startswith("renderD")]
            except OSError:
                render_nodes = []
            usable = [n for n in render_nodes
                     if os.access(os.path.join(dri, n), os.R_OK | os.W_OK)]
            if usable:
                yield line("INFO", f"{dri}: present, {usable[0]} readable+writable by "
                              f"user {user}")
            else:
                yield line("INFO", f"{dri}: present but no renderD* readable+writable "
                              f"by user {user} — fix: sudo usermod -aG "
                              f"render,video {user} (needed only for "
                              f"seinn-convert hardware encoding, not for "
                              f"playback)")

    # ---- 6. auth token ----------------------------------------------------
    if doctor_cfg is None:
        yield line("SKIP", f"auth token: {skip_reason}")
    else:
        token = doctor_cfg["auth_token"]
        if token:
            yield line("PASS", "auth token: set")
        elif doctor_cfg["delete_enabled"]:
            yield line("FAIL", "auth token: empty with delete_enabled = true — "
                          "any LAN client can delete files — fix: set "
                          "auth_token in the config")
        else:
            yield line("WARN", "auth token: gate open — any LAN client can save "
                          "progress")

    # ---- 7. state_db --------------------------------------------------
    if doctor_cfg is None:
        yield line("SKIP", f"state_db: {skip_reason}")
    else:
        db_path = doctor_cfg["state_db"]
        if os.path.exists(db_path):
            try:
                # mode=ro: a plain connect() would create the file if it
                # were missing — never appropriate for a read-only check.
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.execute("SELECT 1").fetchone()
                conn.close()
                yield line("PASS", f"state_db: {db_path} opened read-only, "
                              f"query ok")
            except sqlite3.Error as exc:
                yield line("FAIL", f"state_db: {db_path} unreadable: {exc}")
        else:
            parent = os.path.dirname(db_path) or "."
            if os.access(parent, os.W_OK):
                yield line("PASS", f"state_db: {db_path} absent, will be created "
                              f"on first write (parent {parent} writable)")
            else:
                fix = (f"sudo chown {user} {parent} (or setfacl -m "
                       f"u:{user}:rwX {parent})")
                yield line("FAIL", f"state_db: {db_path} absent and parent "
                              f"{parent} not writable by user {user} — "
                              f"fix: {fix}")

    # ---- 8. service (launchd daemon / systemd unit) ------------------------
    if IS_DARWIN:
        # Dispatches first — Darwin never falls into the systemctl-absent
        # SKIP branch below, systemctl doesn't exist on this platform at all.
        label = launchd_label(service_name)
        plist_path = f"/Library/LaunchDaemons/{label}.plist"
        if not os.path.exists(plist_path):
            yield line("FAIL", f"launchd: plist {plist_path} not found")
        else:
            active = _doctor_service_active(service_name)
            if active:
                yield line("PASS", f"launchd: {label} loaded and running")
            else:
                n_roots = len(doctor_cfg["roots"]) if doctor_cfg else 0
                if n_roots > 0:
                    try:
                        probe = subprocess.run(
                            ["launchctl", "print", f"system/{label}"],
                            capture_output=True, text=True, timeout=5)
                        loaded = probe.returncode == 0
                    except (OSError, subprocess.TimeoutExpired):
                        loaded = False
                    if loaded:
                        fix = f"sudo launchctl kickstart -k system/{label}"
                    else:
                        fix = f"sudo launchctl bootstrap system {plist_path}"
                    yield line("WARN", f"launchd: {label} inactive — fix: {fix}")
                else:
                    yield line("INFO", f"launchd: {label} inactive (no roots "
                                  f"configured — install.sh deliberately "
                                  f"doesn't bootstrap a rootless agent — it "
                                  f"would crash-loop)")
    elif shutil.which("systemctl") is None:
        yield line("SKIP", "systemd: no systemd — fine for --no-service installs")
    else:
        unit_path = f"/etc/systemd/system/{service_name}.service"
        active = _doctor_service_active(service_name)
        if not os.path.exists(unit_path):
            yield line("FAIL", f"systemd: unit file {unit_path} not found")
        elif active:
            yield line("PASS", f"systemd: {service_name} unit active")
        else:
            n_roots = len(doctor_cfg["roots"]) if doctor_cfg else 0
            if n_roots > 0:
                yield line("WARN", f"systemd: {service_name} inactive — fix: "
                              f"sudo systemctl start {service_name}")
            else:
                yield line("INFO", f"systemd: {service_name} inactive (no roots "
                              f"configured — install.sh deliberately "
                              f"doesn't start a rootless agent — it would "
                              f"crash-loop)")

    # ---- 9. libkrutho.so ---------------------------------------------
    # Never a failure — the agent must never fail for auth's future being
    # missing, and neither does this check.
    lib_path = (doctor_cfg["krutho_lib"] if doctor_cfg else
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "libkrutho.so"))
    if load_krutho(lib_path) is not None:
        yield line("INFO", f"libkrutho.so: loaded ({lib_path})")
    else:
        yield line("INFO", "libkrutho.so: absent (bridging mode only)")


def doctor_verdict(results):
    """-> (exit_code, verdict_line): 0/'DOCTOR: PASS' or
    1/'DOCTOR: ISSUES (<n> fail, <m> warn)'. Counts FAIL/WARN only."""
    n_fail = sum(1 for r in results if r.level == "FAIL")
    n_warn = sum(1 for r in results if r.level == "WARN")
    if n_fail:
        return 1, f"DOCTOR: ISSUES ({n_fail} fail, {n_warn} warn)"
    return 0, "DOCTOR: PASS"


def run_doctor(config_path, service_name):
    """Read-only diagnosis of a deployed install. Prints one PASS/FAIL/WARN/
    INFO/SKIP line per check, ending with a DOCTOR: PASS/ISSUES verdict.
    Returns the process exit code (0 when no FAIL lines, 1 otherwise — WARN
    alone does not fail the exit code, a token-less LAN install is a choice
    not a breakage)."""
    results = []
    for r in doctor_checks(config_path, service_name):
        results.append(r)
        print(f"{r.level}  {r.message}" + (f" — fix: {r.fix}" if r.fix else ""))
    code, verdict = doctor_verdict(results)
    print(verdict)
    return code


def linux_btimes(paths):
    """Birth times via coreutils stat (statx); None where unknown."""
    out = {}
    for i in range(0, len(paths), STAT_BATCH):
        chunk = paths[i:i + STAT_BATCH]
        try:
            res = subprocess.run(["stat", "-c", "%W", "--", *chunk],
                                 capture_output=True, text=True, timeout=30)
            lines = res.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            lines = []
        if len(lines) != len(chunk):  # vanished file mid-listing skews order
            continue
        for path, line in zip(chunk, lines):
            try:
                epoch = int(line)
                out[path] = epoch if epoch > 0 else None
            except ValueError:
                out[path] = None
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"seinn-agent/{AGENT_VERSION}"
    cfg = None  # set on the class at startup

    # ---- plumbing ----------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def handle(self):
        # A video client aborts range requests constantly (seeks, prefetch
        # cancellation, app backgrounding). Unhandled, each one dumps a
        # traceback into the service log and buries the real errors.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def read_body(self):
        """Consume the request body. Must run before any reply to a request
        that has one — including a rejection.

        In HTTP/1.1 the body is part of the message frame, and this server
        keeps connections alive. Answering without reading leaves those bytes
        in the socket, so the *next* request on that connection is parsed
        starting mid-body: Python takes the JSON tail as the method name and
        replies 501 Unsupported method. Observed 2026-07-30 as request lines
        like `{"position":629.75,...}POST /api/progress HTTP/1.1` — a rejected
        write breaking unrelated reads. The 401 path in handle_post_progress
        was returning before the read; now nothing can, because dispatch
        drains first.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY:
            # Oversized: still drain it, or the connection is just as poisoned
            # as if we'd ignored it.
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                remaining -= len(block)
            return b""
        return self.rfile.read(length)

    def send_json(self, obj, status=HTTPStatus.OK, no_store=False):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        # no-store: a failure must never be cached. A thumb that couldn't be
        # generated once would otherwise leave that row permanently blank in
        # any HTTP cache downstream.
        self.send_json({"error": message}, status, no_store=True)

    AUTH_HEADER = "X-Seinn-Token"

    def authorized(self):
        """Gate for state-changing requests.

        Interim measure (2026-07-30) until Krutho credentials land: a shared
        token, constant-time compared. Reads stay open — on this LAN the
        asymmetry that matters is that a stray client can *delete*. With no
        token configured the gate is open and startup says so loudly.

        Phase C (2026-08-15) adds a second path: the presented value may
        instead be a valid, unexpired HMAC session token minted by
        POST /api/session. Path one (the exact compare above) is untouched —
        existing installs and the deployed TVs keep working with zero client
        changes. Path two is checked only when path one fails, so it costs
        nothing on the common case.
        """
        expected = self.cfg["auth_token"]
        if not expected:
            return True
        presented = self.headers.get(self.AUTH_HEADER, "")
        if hmac.compare_digest(presented, expected):
            return True
        return verify_session_token(self.cfg, presented) is not None

    def resolve(self, root, relpath):
        """Return absolute path inside root, or None on traversal/unknown."""
        base = self.cfg["roots"].get(root)
        if base is None:
            return None
        full = os.path.realpath(os.path.join(base, relpath))
        if full != base and not full.startswith(base + os.sep):
            return None
        return full

    def split_file_url(self, path):
        parts = unquote(path).split("/", 3)  # '', 'files', root, relpath
        if len(parts) < 3 or parts[1] != "files":
            return None, None
        return parts[2], parts[3] if len(parts) == 4 else ""

    # ---- listings ----------------------------------------------------

    def sole_root(self):
        roots = self.cfg["roots"]
        return next(iter(roots)) if len(roots) == 1 else None

    def handle_list(self, query):
        q = parse_qs(query)
        root = q.get("root", [None])[0] or self.sole_root()
        rel = q.get("path", [""])[0]
        if root is None:
            return self.send_error_json(HTTPStatus.BAD_REQUEST,
                                        "root required (several configured)")
        dirpath = self.resolve(root, rel)
        if dirpath is None or not os.path.isdir(dirpath):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "no such directory")

        if q.get("recursive", ["0"])[0] in ("1", "true", "yes"):
            return self.send_recursive_list(root, rel, dirpath)

        names = sorted(os.listdir(dirpath))
        if self.cfg["hide_dotfiles"]:
            names = [n for n in names if not n.startswith(".")]
        entries, need_btime = [], []
        for name in names:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            is_dir = os.path.isdir(full)
            ext = "dir" if is_dir else os.path.splitext(name)[1].lstrip(".").lower()
            btime = int(st.st_birthtime) if HAVE_BIRTHTIME else None
            entry = {"name": name, "is_dir": is_dir,
                     "size": st.st_size, "mtime": int(st.st_mtime),
                     "btime": btime, "type": ext}
            if is_dir:
                entry["count"] = self.child_count(full)
            entries.append(entry)
            if not HAVE_BIRTHTIME:
                need_btime.append(full)
        if need_btime:
            btimes = linux_btimes(need_btime)
            for entry, full in zip(entries, need_btime):
                entry["btime"] = btimes.get(full)
        # Carried in the listing rather than fetched per row: a 4,000-entry
        # folder would otherwise mean 4,000 extra requests.
        progress = self.progress_rows(root, rel)
        durations = self.duration_rows(root, rel)
        for entry in entries:
            if not entry["is_dir"]:
                entry["duration"] = durations.get(entry["name"])
                if entry["name"] in progress:
                    entry["progress"] = progress[entry["name"]]
        self.send_json({"root": root, "path": rel, "entries": entries})

    def send_recursive_list(self, root, rel, dirpath):
        """Flattened subtree for shuffle: files only, names relative to `rel`.

        No directory entries, no counts, and btime is null — nothing sorts a
        shuffled list, and skipping btime avoids the batched coreutils stat
        shell-out per request. Dot-directories are pruned from descent to
        match the flat listing's dot-file filter; symlinked directories are
        not followed (os.walk default) for cycle safety — content reachable
        only through a symlinked directory is therefore invisible to shuffle.
        """
        hide = self.cfg["hide_dotfiles"]
        entries = []
        for cur, dirs, files in os.walk(dirpath):
            if hide:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            dirs.sort()
            for name in sorted(files):
                if hide and name.startswith("."):
                    continue
                full = os.path.join(cur, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    "name": os.path.relpath(full, dirpath),
                    "is_dir": False,
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    "btime": None,
                    "type": os.path.splitext(name)[1].lstrip(".").lower()})
        progress = self.progress_rows(root, rel, recursive=True)
        durations = self.duration_rows(root, rel, recursive=True)
        for entry in entries:
            entry["duration"] = durations.get(entry["name"])
            if entry["name"] in progress:
                entry["progress"] = progress[entry["name"]]
        self.send_json({"root": root, "path": rel, "entries": entries})

    # ---- watched state -----------------------------------------------

    def child_count(self, path):
        """How many entries a browse of this folder would actually show.

        Counts what the listing would return, not what's on disk: merged_daily
        holds 84 real files next to 410 macOS `._` sidecars, so the raw number
        would be off by 5x and useless as a "how much is in here" signal.
        scandir doesn't stat, so this stays cheap even on the big folders.
        """
        try:
            with os.scandir(path) as it:
                if self.cfg["hide_dotfiles"]:
                    return sum(1 for e in it if not e.name.startswith("."))
                return sum(1 for _ in it)
        except OSError:
            return None

    def thumb_cache_bytes(self):
        """Total bytes of cached thumbnail files, for Settings. None if the
        cache dir doesn't exist yet (thumbs never generated, or disabled) —
        distinguished from 0 (dir exists, empty) so the UI doesn't claim an
        empty cache when there's really no cache at all. The dir is flat
        (make_thumb writes straight into cache_dir, no subdirectories) but
        os.walk is used anyway rather than assuming that never changes; a
        single bad entry degrades the total to None instead of failing
        /api/roots."""
        cache_dir = self.cfg["cache_dir"]
        if not os.path.isdir(cache_dir):
            return None
        total = 0
        try:
            for cur, _dirs, files in os.walk(cache_dir):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(cur, name))
                    except OSError:
                        continue
        except OSError:
            return None
        return total

    def progress_rows(self, root, rel, recursive=False):
        """Stored rows for files directly inside one directory — or, with
        recursive, for the whole subtree, keyed by path relative to `rel`."""
        prefix = f"{rel}/" if rel else ""
        # LIKE treats % and _ as wildcards, and folder names carry _ freely
        # (merged_daily) — unescaped, a lookalike sibling's rows would attach
        # to the wrong files. ESCAPE makes the prefix literal.
        like = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        try:
            conn = db_connect(self.cfg["state_db"])
        except sqlite3.Error:
            return {}
        try:
            cur = conn.execute(
                "SELECT path, position, duration, watched FROM progress "
                "WHERE root = ? AND path LIKE ? ESCAPE '\\'", (root, like))
            out = {}
            for path, position, duration, watched in cur:
                name = path[len(prefix):]
                if not recursive and "/" in name:   # deeper than this directory
                    continue
                out[name] = {"position": position, "duration": duration,
                             "watched": bool(watched)}
            return out
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def duration_rows(self, root, rel, recursive=False):
        """Stored durations for files directly inside one directory — or,
        with recursive, for the whole subtree, keyed by path relative to
        `rel`. Rows with a NULL duration (probe pending or failed) are
        omitted."""
        prefix = f"{rel}/" if rel else ""
        like = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        try:
            conn = db_connect(self.cfg["state_db"])
        except sqlite3.Error:
            return {}
        try:
            cur = conn.execute(
                "SELECT path, duration FROM durations WHERE root = ? AND "
                "path LIKE ? ESCAPE '\\' AND duration IS NOT NULL", (root, like))
            out = {}
            for path, duration in cur:
                name = path[len(prefix):]
                if not recursive and "/" in name:   # deeper than this directory
                    continue
                out[name] = duration
            return out
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def handle_get_progress(self, query):
        q = parse_qs(query)
        root = q.get("root", [None])[0] or self.sole_root()
        rel = q.get("path", [""])[0]
        if root is None or not rel:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "root and path required")
        try:
            conn = db_connect(self.cfg["state_db"])
            row = conn.execute("SELECT position, duration, watched FROM progress "
                               "WHERE root = ? AND path = ?", (root, rel)).fetchone()
            conn.close()
        except sqlite3.Error as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        if row is None:
            return self.send_json({"position": 0, "duration": 0, "watched": False})
        self.send_json({"position": row[0], "duration": row[1], "watched": bool(row[2])})

    def handle_post_progress(self):
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED,
                                        "missing or bad " + self.AUTH_HEADER)
        try:
            body = json.loads(self.body or b"{}")
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad JSON body")

        root = body.get("root") or self.sole_root()
        rel = body.get("path") or ""
        if root not in self.cfg["roots"] or not rel:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "root and path required")

        try:
            conn = db_connect(self.cfg["state_db"])
            if body.get("reset"):
                conn.execute("DELETE FROM progress WHERE root = ? AND path = ?", (root, rel))
                conn.commit()
                conn.close()
                return self.send_json({"position": 0, "duration": 0, "watched": False})

            position = float(body.get("position", 0) or 0)
            duration = float(body.get("duration", 0) or 0)
            if "watched" in body:
                watched = bool(body["watched"])
                # Marking watched by hand shouldn't strand a stale resume point.
                if watched:
                    position = duration
                else:
                    position = 0.0
            else:
                watched = duration > 0 and position / duration >= WATCHED_FRACTION
            conn.execute(
                "INSERT INTO progress (root, path, position, duration, watched, updated) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(root, path) DO UPDATE SET "
                "position=excluded.position, duration=excluded.duration, "
                "watched=excluded.watched, updated=excluded.updated",
                (root, rel, position, duration, int(watched), int(time.time())))
            conn.commit()
            conn.close()
        except (sqlite3.Error, ValueError) as exc:
            return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        self.send_json({"position": position, "duration": duration, "watched": watched})

    # ---- sessions (Phase C + D) ----------------------------------------

    # KRUTHO_AUDIENCE default lives in cfg["krutho_audience"] (set in
    # load_config; overridable only by --test-accept-audience, TEST-ONLY).
    NONCE_TTL = 120

    def handle_get_session_challenge(self):
        """Issue a single-use nonce for a Krutho presentation.

        Nonce/audience are how krutho_verify kills replay — the presentation
        the phone builds is bound to this exact nonce, and this nonce is
        consumed (removed) the moment a POST /api/session verify attempt
        touches it, successful or not. In-memory dict, purged opportunistically
        on each issue — no cleanup thread, same doctrine as the rest of the
        agent's ephemeral state.
        """
        now = int(time.time())
        nonce = secrets.token_urlsafe(32)
        expires = now + self.NONCE_TTL
        with self.cfg["nonces_lock"]:
            # Opportunistic purge: bounded by how often challenges are
            # issued, not a background thread.
            expired = [n for n, exp in self.cfg["nonces"].items() if exp < now]
            for n in expired:
                del self.cfg["nonces"][n]
            self.cfg["nonces"][nonce] = expires
        self.send_json({"nonce": nonce, "audience": self.cfg["krutho_audience"],
                        "expires": expires}, no_store=True)

    def handle_post_session(self):
        """Exchange a credential for a short-lived session token.

        Two body shapes on one endpoint:
          - Bridging mode: {"token": "<interim shared token>"} checked
            against auth_token, constant-time, same as authorized()'s path
            one.
          - Phase D credential mode: {"vp_token", "presentation_submission",
            "nonce"} verified against libkrutho.so via ctypes. Bridging
            keeps working alongside it until Phase E retires the interim
            token.
        """
        try:
            body = json.loads(self.body or b"{}")
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad JSON body")

        if "vp_token" in body:
            return self.handle_post_session_krutho(body)

        presented = body.get("token")
        expected = self.cfg["auth_token"]
        if (not isinstance(presented, str) or not expected
                or not hmac.compare_digest(presented, expected)):
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "bad token")
        token, expires = mint_session_token(self.cfg, "bridge")
        self.send_json({"session_token": token, "expires": expires}, no_store=True)

    def handle_post_session_krutho(self, body):
        """Phase D: verify an OID4VP presentation via ctypes and issue a
        session token whose subject is a claim from claims_json.

        Subject claim key: the fixture credential discloses {"loyaltyId":
        "SM-123456"} — no "sub" field — confirmed empirically against the
        SDK's own fixture (D2), not assumed. A real Krutho credential's
        claim shape is [issuer-gated] and may differ; this is the honest
        ceiling reachable without issuer infrastructure.
        """
        lib = self.cfg["krutho"]
        if lib is None:
            return self.send_error_json(HTTPStatus.UNAUTHORIZED,
                                        "KRUTHO_UNAVAILABLE: libkrutho.so not loaded "
                                        "on this agent (bridging mode only)")

        vp_token = body.get("vp_token")
        submission = body.get("presentation_submission")
        nonce = body.get("nonce")
        if not all(isinstance(x, str) and x for x in (vp_token, submission, nonce)):
            return self.send_error_json(HTTPStatus.BAD_REQUEST,
                                        "vp_token, presentation_submission, nonce required")

        # Single-use: the nonce is consumed on this attempt regardless of
        # verify outcome — a replay of the same presentation must fail even
        # if the first attempt failed for an unrelated reason.
        with self.cfg["nonces_lock"]:
            expiry = self.cfg["nonces"].pop(nonce, None)
        if expiry is None or expiry < int(time.time()):
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, "INVALID_NONCE")

        rc, claims = krutho_verify(
            lib, vp_token, submission,
            self.cfg["krutho_trust_chain_bytes"],
            self.cfg["krutho_revocation_list_bytes"],
            nonce, self.cfg["krutho_audience"])

        if rc != KRUTHO_OK:
            error_name = KRUTHO_ERROR_NAMES.get(rc, str(rc))
            # Diagnostic gold, not secret, on this LAN — comment in the
            # design doc states this explicitly.
            return self.send_error_json(HTTPStatus.UNAUTHORIZED, error_name)

        subject_value = (claims or {}).get("loyaltyId")
        subject = f"credential:{subject_value}" if subject_value else "credential:unknown"
        token, expires = mint_session_token(self.cfg, subject)
        self.send_json({"session_token": token, "expires": expires}, no_store=True)

    # ---- enrolment (Phase E) ------------------------------------------

    def credential_backed_subject(self):
        """Returns the subject string if X-Seinn-Token is a valid,
        unexpired session token whose subject is credential-backed
        (starts with "credential:") — the interim shared token and plain
        bridging sessions do NOT count. This is the enforcement point for
        "endorsement is the whole security model": only a phone that has
        actually presented a Krutho credential may approve a TV's
        enrolment. --test-treat-bridging-as-credential (DEBUG-only) widens
        this to also accept subject == "bridge", so the flow is
        exercisable on this Mac without libkrutho.so."""
        presented = self.headers.get(self.AUTH_HEADER, "")
        subject = verify_session_token(self.cfg, presented)
        if subject is None:
            return None
        if subject.startswith("credential:"):
            return subject
        if self.cfg["test_treat_bridging_as_credential"] and subject == "bridge":
            return subject
        return None

    def handle_post_enrol_start(self):
        """No auth — the TV has nothing yet (no credential, no session).
        Crude cap on outstanding enrolments; comment in enrol_start states
        this is LAN posture, not a real rate limiter."""
        got = enrol_start(self.cfg)
        if got is None:
            return self.send_error_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"too many outstanding enrolments (cap {MAX_OUTSTANDING_ENROL})")
        enrol_id, code = got
        self.send_json({"enrol_id": enrol_id, "code": code}, no_store=True)

    def handle_post_enrol_approve(self):
        subject = self.credential_backed_subject()
        if subject is None:
            return self.send_error_json(
                HTTPStatus.UNAUTHORIZED,
                "enrolment approval requires a credential-backed session "
                "token — the interim shared token does not approve "
                "enrolments (endorsement is the whole security model)")
        try:
            body = json.loads(self.body or b"{}")
        except ValueError:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "bad JSON body")
        code = body.get("code")
        if not isinstance(code, str) or not code:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "code required")
        if not enrol_approve(self.cfg, code, subject):
            return self.send_error_json(HTTPStatus.NOT_FOUND,
                                        "no matching outstanding enrolment")
        self.send_json({"approved": True}, no_store=True)

    def handle_get_enrol_poll(self, query):
        q = parse_qs(query)
        enrol_id = q.get("enrol_id", [None])[0]
        if not enrol_id:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "enrol_id required")
        status, payload = enrol_poll(self.cfg, enrol_id)
        if status == "unknown":
            return self.send_error_json(HTTPStatus.NOT_FOUND,
                                        "unknown or expired enrol_id")
        if status == "pending":
            return self.send_json({"status": "pending"}, no_store=True)
        token, expires = payload
        self.send_json({"session_token": token, "expires": expires}, no_store=True)

    # ---- thumbnails --------------------------------------------------

    def handle_thumb(self, query):
        if not self.cfg["thumbs_enabled"]:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "thumbs disabled")
        if not HAVE_FFMPEG:
            return self.send_error_json(
                HTTPStatus.NOT_FOUND,
                "thumbnails unavailable: ffmpeg not installed on the server")
        q = parse_qs(query)
        root = q.get("root", [None])[0] or self.sole_root()
        rel = q.get("path", [""])[0]
        full = self.resolve(root, rel) if root else None
        if full is None or not os.path.isfile(full):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "no such file")

        st = os.stat(full)
        key = hashlib.sha1(
            f"{full}|{int(st.st_mtime)}|{st.st_size}|{THUMB_WIDTH}".encode()
        ).hexdigest()
        cache = os.path.join(self.cfg["cache_dir"], key + ".jpg")
        if not os.path.exists(cache):
            if not self.make_thumb(full, cache):
                return self.send_error_json(HTTPStatus.NOT_FOUND,
                                            "no frame extractable")
        try:
            with open(cache, "rb") as f:
                blob = f.read()
        except OSError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "cache read failed")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(blob)))
        # cache key includes mtime+size, so a given URL's bytes never change
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(blob)

    def make_thumb(self, full, cache):
        os.makedirs(self.cfg["cache_dir"], exist_ok=True)
        tmp = f"{cache}.{os.getpid()}.{threading.get_ident()}.tmp.jpg"
        with THUMB_SLOTS:
            for offset in THUMB_OFFSETS:
                seek = ["-ss", str(offset)] if offset is not None else []
                cmd = ["ffmpeg", "-nostdin", "-v", "error", *seek,
                       "-i", full, "-frames:v", "1",
                       "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "5",
                       "-f", "mjpeg", "-y", tmp]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=60)
                except (OSError, subprocess.TimeoutExpired):
                    break
                if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    os.replace(tmp, cache)  # atomic: readers see whole file
                    return True
        if os.path.exists(tmp):
            os.remove(tmp)
        return False

    # ---- files -------------------------------------------------------

    def handle_file(self, head_only=False):
        root, rel = self.split_file_url(urlparse(self.path).path)
        if root is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "bad file url")
        full = self.resolve(root, rel)
        if full is None or not os.path.isfile(full):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "no such file")

        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if rng:
            unit, _, spec = rng.partition("=")
            first, _, last = spec.partition("-")
            if unit.strip() != "bytes" or (not first and not last) or "," in spec:
                return self.send_error_json(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "bad range")
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:  # suffix: last N bytes
                start = max(0, size - int(last))
            end = min(end, size - 1)
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status is HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        # Log throughput per response: the only way to see what a client's link
        # is actually delivering, which is what distinguishes "the server is
        # slow" from "this player is on a thin Wi-Fi link".
        sent = 0
        started = time.monotonic()
        ttfb = None
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = f.read(min(CHUNK, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    if ttfb is None:
                        ttfb = time.monotonic() - started
                    sent += len(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass  # players abort range requests constantly; not an error
        elapsed = time.monotonic() - started
        # One line per range request, with the offset and byte count. Without
        # those two fields there is no way to tell an index read from a
        # streaming read, and that is precisely the difference between "this
        # file is slow to open" and "this file is playing normally". The XFER
        # line below is size-gated, so it records only the big transfers and is
        # structurally blind to the small requests that make an open slow — an
        # analysis of three days of logs on 2026-07-31 could not separate the
        # two phases for exactly this reason.
        print(f"{self.address_string()} REQ off={start} len={end - start + 1} "
              f"sent={sent} ttfb={(ttfb or 0) * 1000:.1f} tot={elapsed * 1000:.1f} "
              f"{os.path.basename(full)}", flush=True)
        if sent >= 4 * CHUNK and elapsed > 0.05:
            mbps = sent * 8 / elapsed / 1e6
            print(f"{self.address_string()} XFER {sent/1e6:.1f} MB in "
                  f"{elapsed:.1f}s = {mbps:.0f} Mbps  {os.path.basename(full)}",
                  flush=True)

    # ---- verbs -------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/roots":
            # name + path: the client shows the real path under each share name
            # so it's obvious what you're opening. free_bytes/entry_count are
            # per-root and degrade to None on error rather than failing the
            # whole endpoint — this is the app's first request, it must not
            # 500 because one mount is flaky. entry_count mirrors child_count
            # (same dot-file filtering) rather than walking the whole tree.
            roots = []
            for n, p in sorted(self.cfg["roots"].items()):
                try:
                    free_bytes = shutil.disk_usage(p).free
                except OSError:
                    free_bytes = None
                roots.append({"name": n, "path": p, "free_bytes": free_bytes,
                              "entry_count": self.child_count(p)})
            self.send_json({"roots": roots,
                            "agent_version": AGENT_VERSION,
                            "thumb_cache_bytes": self.thumb_cache_bytes()})
        elif url.path == "/api/list":
            self.handle_list(url.query)
        elif url.path == "/api/thumb":
            self.handle_thumb(url.query)
        elif url.path == "/api/progress":
            self.handle_get_progress(url.query)
        elif url.path == "/api/session/challenge":
            self.handle_get_session_challenge()
        elif url.path == "/api/enrol/poll":
            self.handle_get_enrol_poll(url.query)
        elif url.path.startswith("/files/"):
            self.handle_file()
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_POST(self):
        # Drain before dispatch, never inside a handler: that way an early
        # return (auth, bad route) can't desynchronise the connection.
        self.body = self.read_body()
        path = urlparse(self.path).path
        if path == "/api/progress":
            self.handle_post_progress()
        elif path == "/api/session":
            self.handle_post_session()
        elif path == "/api/enrol/start":
            self.handle_post_enrol_start()
        elif path == "/api/enrol/approve":
            self.handle_post_enrol_approve()
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_HEAD(self):
        if urlparse(self.path).path.startswith("/files/"):
            self.handle_file(head_only=True)
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_DELETE(self):
        if not self.authorized():
            return self.send_error_json(HTTPStatus.UNAUTHORIZED,
                                        "missing or bad " + self.AUTH_HEADER)
        if not self.cfg["delete_enabled"]:
            return self.send_error_json(HTTPStatus.FORBIDDEN,
                                        "delete disabled by config")
        root, rel = self.split_file_url(urlparse(self.path).path)
        full = self.resolve(root, rel) if root else None
        if full is None or not os.path.exists(full) or not rel:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "no such path")
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        self.send_json({"deleted": f"{root}/{rel}"})


def main():
    cfg = load_config(sys.argv[1:])
    Handler.cfg = cfg
    server = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    server.daemon_threads = True
    threading.Thread(target=duration_sweep_loop, args=(cfg,), daemon=True).start()
    roots = ", ".join(f"{n}={p}" for n, p in cfg["roots"].items())
    ffmpeg_status = "yes" if HAVE_FFMPEG else "MISSING (thumbs+durations off)"
    krutho_status = ("loaded" if cfg["krutho"] is not None
                      else "ABSENT (bridging mode only)")
    print(f"seinn-agent {AGENT_VERSION} on {cfg['bind']}:{cfg['port']} roots: {roots} "
          f"delete_enabled={cfg['delete_enabled']} "
          f"auth={'token' if cfg['auth_token'] else 'NONE'} "
          f"session=hmac(ttl={cfg['session_ttl']}) "
          f"krutho={krutho_status} "
          f"ffmpeg={ffmpeg_status}", flush=True)
    if cfg["delete_enabled"] and not cfg["auth_token"]:
        print("WARNING: delete is enabled with no auth_token — any client on "
              "this network can delete files served by this agent", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
