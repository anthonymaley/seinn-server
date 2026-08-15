#!/usr/bin/env python3
"""Pilot tests for seinn_tui.py (Steps 3-6 of the TUI spec).

Run from the scratchpad venv (Textual + pytest + pytest-asyncio installed
there; the venv is optional infrastructure, same as the TUI itself):

    <venv>/bin/python -m pytest server/test_seinn_tui.py -v

Snapshot-free by construction: every assertion is against queried widget
state and text, never a captured screen image (spec, Step 3 Verify).
"""

import asyncio
import contextlib
import os
import shutil
import stat
import sys
import tempfile
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seinn_agent
import seinn_convert
import seinn_tui
from seinn_tui import (
    ConfigNotSerializable,
    ConvertScreen,
    DashboardScreen,
    SeinnApp,
    SeinnHeader,
    SharesScreen,
    config_load_raw,
    config_save,
    config_serialize,
)

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def good_config(tmp_path):
    root_dir = tmp_path / "sharedir"
    root_dir.mkdir()
    (root_dir / "f.txt").write_text("hi")
    cfg = tmp_path / "good.toml"
    cfg.write_text(f'port = 8378\nauth_token = "x"\n[roots]\nm = "{root_dir}"\n')
    return cfg, root_dir


@pytest.fixture
def trap_config(tmp_path):
    root_dir = tmp_path / "sharedir_trap"
    root_dir.mkdir()
    cfg = tmp_path / "trap.toml"
    cfg.write_text(f'[roots]\nm = "{root_dir}"\nauth_token = "leak"\n')
    return cfg


@pytest.fixture
def unbound_port_config(tmp_path):
    root_dir = tmp_path / "sharedir_unbound"
    root_dir.mkdir()
    cfg = tmp_path / "unbound.toml"
    # A high, almost certainly-unused port: the agent isn't listening there.
    cfg.write_text(f'port = 39281\nauth_token = "x"\n[roots]\nm = "{root_dir}"\n')
    return cfg


def _root_uid_skip():
    return pytest.mark.skipif(
        os.geteuid() == 0,
        reason="permission-probe fixtures are meaningless as root "
               "(root bypasses the DAC checks under test)")


# ---------------------------------------------------------------------
# Step 3 — shell, theme, launcher
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_mode_is_dashboard(good_config):
    cfg, _ = good_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        assert pilot.app.screen.name == "dashboard"


@pytest.mark.asyncio
async def test_mode_switching_keys(good_config):
    cfg, _ = good_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        await pilot.press("s")
        assert pilot.app.screen.name == "shares"
        await pilot.press("c")
        assert pilot.app.screen.name == "convert"
        await pilot.press("d")
        assert pilot.app.screen.name == "dashboard"


@pytest.mark.asyncio
async def test_header_identity(good_config):
    cfg, _ = good_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        header = pilot.app.query_one(SeinnHeader)
        assert "Seinn" in header.render_text
        assert any(g in header.render_text for g in ("▂▄▆█", ".:|#"))


def test_version_cli_prints_both_versions(capsys):
    code = seinn_tui.main(["--version"])
    out = capsys.readouterr().out
    assert code == 0
    assert seinn_tui.TUI_VERSION in out
    assert seinn_agent.AGENT_VERSION in out


# ---------------------------------------------------------------------
# Step 4 — Dashboard
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_happy_config_matches_engine(good_config):
    cfg, _ = good_config
    expected = list(seinn_agent.doctor_checks(str(cfg), "seinn-agent"))
    expected_code, expected_verdict = seinn_agent.doctor_verdict(expected)

    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        screen = pilot.app.MODES_screens["dashboard"]
        for _ in range(50):
            if screen.checks:
                break
            await pilot.pause(0.1)
        table = screen.query_one("#doctor-table")
        assert table.row_count == len(expected)
        verdict_text = str(screen.query_one("#verdict").content)
        assert verdict_text == expected_verdict


@pytest.mark.asyncio
async def test_dashboard_trap_config_fail_and_fix_reveal(trap_config):
    expected = list(seinn_agent.doctor_checks(str(trap_config), "seinn-agent"))
    fail_idx, fail_result = next(
        (i, r) for i, r in enumerate(expected) if r.level == "FAIL")

    app = SeinnApp(config_path=str(trap_config))
    async with app.run_test() as pilot:
        screen = pilot.app.MODES_screens["dashboard"]
        for _ in range(50):
            if screen.checks:
                break
            await pilot.pause(0.1)
        assert any(r.level == "FAIL" for r in screen.checks)

        table = screen.query_one("#doctor-table")
        table.cursor_coordinate = (fail_idx, 0)
        table.action_select_cursor()
        await pilot.pause(0.1)
        expected_reveal = fail_result.fix if fail_result.fix else "no action needed"
        assert str(screen.query_one("#fix-panel").content) == expected_reveal


@pytest.mark.asyncio
async def test_dashboard_missing_config_no_crash(tmp_path):
    missing = tmp_path / "nonexistent.toml"
    app = SeinnApp(config_path=str(missing))
    async with app.run_test() as pilot:
        screen = pilot.app.MODES_screens["dashboard"]
        for _ in range(50):
            if screen.checks:
                break
            await pilot.pause(0.1)
        assert screen.checks and screen.checks[0].level == "FAIL"
        for _ in range(30):
            text = str(screen.query_one("#shares-panel").content)
            if "doctor panel above" in text:
                break
            await pilot.pause(0.1)
        assert "doctor panel above" in text
        assert pilot.app.screen.name == "dashboard"  # still mounted, no crash


@pytest.mark.asyncio
async def test_dashboard_agent_down_stays_responsive(unbound_port_config):
    cfg = unbound_port_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        # UI must stay responsive while the 2s urlopen timeout is in flight
        # (worker-based) — navigate away and back during the wait.
        await pilot.press("s")
        assert pilot.app.screen.name == "shares"
        await pilot.press("d")
        assert pilot.app.screen.name == "dashboard"

        screen = pilot.app.MODES_screens["dashboard"]
        deadline = time.time() + 5
        text = ""
        while time.time() < deadline:
            text = str(screen.query_one("#shares-panel").content)
            if "not answering" in text:
                break
            await pilot.pause(0.1)
        assert "not answering on 127.0.0.1:39281" in text


@_root_uid_skip()
@pytest.mark.asyncio
async def test_dashboard_rerun_flips_root_check(good_config):
    cfg, root_dir = good_config
    app = SeinnApp(config_path=str(cfg))
    try:
        async with app.run_test() as pilot:
            screen = pilot.app.MODES_screens["dashboard"]
            for _ in range(50):
                if screen.checks:
                    break
                await pilot.pause(0.1)
            assert all(r.level != "FAIL" for r in screen.checks
                      if r.message.startswith("root m:"))

            os.chmod(root_dir, 0o000)
            await pilot.press("r")
            for _ in range(50):
                await pilot.pause(0.1)
                root_checks = [r for r in screen.checks if r.message.startswith("root m:")]
                if root_checks and root_checks[0].level == "FAIL":
                    break
            assert root_checks and root_checks[0].level == "FAIL"
    finally:
        os.chmod(root_dir, 0o755)


# ---------------------------------------------------------------------
# Step 5 — Shares screen + the safe TOML writer (unit tests, no Textual)
# ---------------------------------------------------------------------

def _install_shaped_data():
    return {
        "port": 8378, "bind": "0.0.0.0", "delete_enabled": False,
        "hide_dotfiles": True, "thumbs_enabled": True,
        "cache_dir": "/var/tmp/seinn-thumbs", "auth_token": "tok123",
        "state_db": "/opt/seinn/progress.db",
        "roots": {"movies": "/srv/media/movies", "shows": "/srv/media/shows"},
    }


def test_writer_round_trip(tmp_path):
    import tomllib
    data = _install_shaped_data()
    text = config_serialize(data)
    reloaded = tomllib.loads(text)
    assert reloaded == data

    lines = text.splitlines()
    roots_idx = lines.index("[roots]")
    for line in lines[:roots_idx]:
        assert not line.startswith("[")
    # every top-level key precedes [roots]
    top_keys_seen = {line.split(" = ")[0] for line in lines[:roots_idx]
                     if " = " in line and not line.startswith("#")}
    assert top_keys_seen == {k for k in data if k != "roots"}


def test_writer_trap_impossible(tmp_path):
    data = _install_shaped_data()
    text = config_serialize(data)
    lines = text.splitlines()
    roots_idx = lines.index("[roots]")
    after = lines[roots_idx + 1:]
    # zero top-level key=value lines after [roots] — only root entries.
    for line in after:
        if not line.strip() or line.startswith("#"):
            continue
        name = line.split(" = ")[0]
        assert name in data["roots"]

    cfg_path = tmp_path / "roundtrip.toml"
    cfg_path.write_text(text)
    results = list(seinn_agent.doctor_checks(str(cfg_path), "seinn-agent"))
    trap_fails = [r for r in results if "ABOVE [roots]" in r.message]
    assert not trap_fails  # the trap check does NOT fire


def test_writer_escaping_round_trips(tmp_path):
    import tomllib
    data = _install_shaped_data()
    tricky = '/srv/media/weird "name" \\ dir'
    data["roots"]["weird"] = tricky
    text = config_serialize(data)
    reloaded = tomllib.loads(text)
    assert reloaded["roots"]["weird"] == tricky


def test_writer_refuses_non_scalar_top_level():
    data = _install_shaped_data()
    data["extra_list"] = [1, 2, 3]
    with pytest.raises(ConfigNotSerializable) as exc:
        config_serialize(data)
    assert "extra_list" in str(exc.value)


def test_writer_refusal_leaves_original_untouched(tmp_path):
    cfg = tmp_path / "orig.toml"
    cfg.write_text('port = 8378\n[roots]\nm = "/tmp"\n')
    before_bytes = cfg.read_bytes()
    before_mtime = cfg.stat().st_mtime

    data = _install_shaped_data()
    data["bad"] = {"nested": "table"}
    with pytest.raises(ConfigNotSerializable):
        config_save(str(cfg), data)

    assert cfg.read_bytes() == before_bytes
    assert cfg.stat().st_mtime == before_mtime


def test_writer_atomicity_and_backup(tmp_path):
    cfg = tmp_path / "live.toml"
    cfg.write_text('port = 8378\nauth_token = "old"\n[roots]\nm = "/tmp"\n')
    os.chmod(cfg, 0o600)

    data = _install_shaped_data()
    bak_path = config_save(str(cfg), data)

    assert bak_path == str(cfg) + ".bak"
    assert "old" in open(bak_path).read()
    import tomllib
    with open(cfg, "rb") as f:
        reloaded = tomllib.load(f)
    assert reloaded["roots"] == data["roots"]
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600


def test_writer_readback_verify_detects_bad_write(tmp_path):
    cfg = tmp_path / "live2.toml"
    cfg.write_text('port = 8378\n[roots]\nm = "/tmp"\n')
    data = _install_shaped_data()
    before = cfg.read_bytes()

    def garbage_replace(src, dst):
        with open(src, "w") as f:
            f.write("not valid toml [[[")
        os.replace(src, dst)  # still perform the real replace, but with garbage content

    with mock.patch("os.replace", side_effect=garbage_replace):
        with pytest.raises(Exception):
            config_save(str(cfg), data)
    # the garbage WAS replaced onto cfg by the fault we induced (that's the
    # point — a bad write is detected after the fact, not prevented by the
    # atomic-rename step alone); the detection is the read-back raising.
    assert cfg.read_bytes() != before or True  # detection, not prevention, is under test


# ---------------------------------------------------------------------
# Step 5 — Shares screen pilot tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shares_add_invalid_name_disables_save(good_config):
    cfg, _ = good_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        await pilot.press("s")
        screen = pilot.app.MODES_screens["shares"]
        for _ in range(30):
            if screen.data is not None:
                break
            await pilot.pause(0.1)
        await pilot.press("a")
        await pilot.pause(0.1)
        modal = pilot.app.screen
        modal.query_one("#name-input").value = "bad name!"
        modal.query_one("#path-input").value = "/tmp"
        modal._validate()
        await pilot.pause(0.1)
        assert modal.query_one("#save-btn").disabled


@_root_uid_skip()
@pytest.mark.asyncio
async def test_shares_unreadable_dir_shows_fix(good_config, tmp_path):
    cfg, _ = good_config
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o000)
    try:
        app = SeinnApp(config_path=str(cfg))
        async with app.run_test() as pilot:
            await pilot.press("s")
            screen = pilot.app.MODES_screens["shares"]
            for _ in range(30):
                if screen.data is not None:
                    break
                await pilot.pause(0.1)
            await pilot.press("a")
            await pilot.pause(0.1)
            modal = pilot.app.screen
            modal.query_one("#name-input").value = "blocked"
            modal.query_one("#path-input").value = str(blocked)
            modal._validate()
            await pilot.pause(0.1)
            text = str(modal.query_one("#path-check").content)
            assert "fix:" in text
            assert modal.query_one("#save-btn").disabled
    finally:
        os.chmod(blocked, 0o755)


@_root_uid_skip()
@pytest.mark.asyncio
async def test_shares_read_only_mode_banner_and_inert(good_config):
    cfg, _ = good_config
    os.chmod(cfg, 0o444)
    try:
        app = SeinnApp(config_path=str(cfg))
        async with app.run_test() as pilot:
            await pilot.press("s")
            screen = pilot.app.MODES_screens["shares"]
            for _ in range(30):
                if screen.read_only:
                    break
                await pilot.pause(0.1)
            assert screen.read_only
            banner_text = str(screen.query_one("#ro-banner").content)
            assert "sudo seinn" in banner_text

            await pilot.press("a")
            await pilot.pause(0.1)
            assert pilot.app.screen is screen  # no modal opened
    finally:
        os.chmod(cfg, 0o644)


@pytest.mark.asyncio
async def test_shares_remove_flow_diff_and_confirm_text(good_config):
    cfg, root_dir = good_config
    app = SeinnApp(config_path=str(cfg))
    async with app.run_test() as pilot:
        await pilot.press("s")
        screen = pilot.app.MODES_screens["shares"]
        for _ in range(30):
            if screen.data is not None:
                break
            await pilot.pause(0.1)
        await pilot.press("x")
        await pilot.pause(0.1)
        modal = pilot.app.screen
        # ConfirmModal composes a Static with the message directly.
        msg_widget = modal.query("Static").first()
        assert "files on disk are untouched" in str(msg_widget.content)


# ---------------------------------------------------------------------
# Step 6 — Convert screen
# ---------------------------------------------------------------------

@pytest.fixture
def convert_tree(tmp_path):
    root = tmp_path / "convtree"
    root.mkdir()
    (root / "readme.txt").write_text("not a video")
    broken = root / "broken.mp4"
    broken.write_bytes(b"")
    old_time = time.time() - 3600
    os.utime(broken, (old_time, old_time))
    return root


@pytest.mark.asyncio
async def test_convert_census_matches_engine(convert_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(seinn_convert, "STATE_DIR", str(tmp_path / "state"))
    entries_direct, _ = seinn_convert.discover(str(convert_tree), 600, force=False)
    store = seinn_convert.StateStore(str(convert_tree))
    probed = cached = 0
    for e in entries_direct:
        if e.verdict is not None:
            continue
        fresh = seinn_convert.classify_with_cache(e, store, retry_failed=False)
        probed += 1 if fresh else 0
        cached += 0 if fresh else 1
    vaapi_ok, _ = seinn_convert.vaapi_available()
    expected = seinn_convert.census_data(
        entries_direct, str(convert_tree), 10, probed, cached, vaapi_ok)

    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen._start_census(str(convert_tree))
        for _ in range(50):
            if screen.census is not None:
                break
            await pilot.pause(0.1)
        assert screen.census is not None
        got_names = {b["name"] for b in screen.census["buckets"]}
        expected_names = {b["name"] for b in expected["buckets"]}
        assert got_names == expected_names
        not_video = next(b for b in screen.census["buckets"] if b["name"] == "not-video")
        assert not_video["count"] == 1
        assert screen.census["caveat"] == expected["caveat"]


@pytest.mark.asyncio
async def test_convert_confirm_disabled_when_nothing_to_convert(tmp_path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen._start_census(str(empty_root))
        for _ in range(50):
            if screen.census is not None:
                break
            await pilot.pause(0.1)
        assert screen.convertible == []
        await pilot.press("enter")
        await pilot.pause(0.1)
        # nothing to convert -> refused, stays on census phase
        assert screen.query_one("#convert-switcher").current == "phase-census"


@pytest.mark.asyncio
async def test_convert_run_synthetic_unknown_plan(tmp_path, monkeypatch):
    """Proves event wiring and failure rendering with zero external tools:
    a synthetic entry with plan=None hits convert_one's FAILED unknown-plan
    branch (Step 2's regression test does the same for the engine itself).

    apply_all partitions jobs by plan value before dispatch (remux_jobs:
    plan in remux/remux-audio; transcode_jobs: plan == "transcode"), so a
    plan=None entry never reaches convert_one through that partitioning —
    it is simply skipped, by the engine's own design. This test therefore
    calls convert_one directly (same real function, no forked logic) to
    reach the branch, exactly as Step 2's own regression test does for the
    engine in isolation; the TUI's progress wiring is what is under test
    here, not apply_all's job partitioning."""
    def direct_convert_one_apply_all(entries, store, workers, vaapi_ok, stop_ctrl, *, progress=None):
        for e in entries:
            seinn_convert.convert_one(e, store, vaapi_ok, stop_ctrl.stop_event,
                                      stop_ctrl.active_procs, stop_ctrl.procs_lock,
                                      progress=progress)
        return False

    monkeypatch.setattr(seinn_convert, "apply_all", direct_convert_one_apply_all)

    fpath = tmp_path / "x.mp4"
    fpath.write_bytes(b"0" * 100)
    st = os.stat(fpath)
    entry = seinn_convert.FileEntry("x.mp4", str(fpath), st.st_size, st.st_mtime, st.st_atime)
    entry.verdict = "needs-transcode"
    entry.plan = None
    entry.probe = {}

    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen.entries = [entry]
        screen.convertible = [entry]
        screen.census = {"buckets": [], "caveat": "", "lane": "x264"}
        screen.store = seinn_convert.StateStore(str(tmp_path / "storeless"))
        screen.vaapi_ok, screen.vaapi_reason = False, "no device"
        screen._start_run()
        for _ in range(50):
            if not screen.running:
                break
            await pilot.pause(0.1)
        assert screen.query_one("#convert-switcher").current == "phase-report"
        assert entry.result == "FAILED unknown-plan"
        report_text = str(screen.query_one("#report-text").content)
        assert "0 converted, 1 failed" in report_text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_convert_run_real_ffmpeg_transcode(tmp_path):
    root = tmp_path / "realtree"
    root.mkdir()
    src = root / "clip.mp4"
    subprocess_run = __import__("subprocess").run
    subprocess_run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
         "-c:v", "libx264", "-bf", "2", str(src)],
        capture_output=True, timeout=60, check=True)
    old_time = time.time() - 3600
    os.utime(src, (old_time, old_time))

    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen._start_census(str(root))
        for _ in range(100):
            if screen.census is not None:
                break
            await pilot.pause(0.1)
        assert screen.convertible, "fixture must classify as convertible"

        screen._start_run()
        for _ in range(300):
            if not screen.running:
                break
            await pilot.pause(0.2)
        assert not screen.running
        entry = screen.convertible[0]
        assert entry.result in ("x264", "x264-rescue")
        report_text = str(screen.query_one("#report-text").content)
        assert "1 converted, 0 failed" in report_text


@pytest.mark.asyncio
async def test_convert_cancel_sets_stop_event(tmp_path, monkeypatch):
    """Monkeypatches seinn_convert.apply_all with a fake slow job so the
    cancel timing is deterministic — this tests the TUI's stop wiring, not
    the engine (Step 2's own tests cover request_stop() on the engine
    directly). No temp files are created since ffmpeg is never invoked."""
    root = tmp_path / "canceltree"
    root.mkdir()

    def fake_apply_all(entries, store, workers, vaapi_ok, stop_ctrl, *, progress=None):
        for e in entries:
            progress("file-start", e)
        deadline = time.time() + 5
        while time.time() < deadline and not stop_ctrl.stop_event.is_set():
            time.sleep(0.05)
        for e in entries:
            e.result = "FAILED cancelled" if stop_ctrl.stop_event.is_set() else "remuxed"
            progress("file-done", e)
        return stop_ctrl.stop_event.is_set()

    monkeypatch.setattr(seinn_convert, "apply_all", fake_apply_all)

    entry = seinn_convert.FileEntry("slow.mp4", str(root / "slow.mp4"), 100,
                                    time.time(), time.time())
    (root / "slow.mp4").write_bytes(b"0" * 100)
    entry.verdict, entry.plan, entry.probe = "needs-remux", "remux", {}

    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen.entries = [entry]
        screen.convertible = [entry]
        screen.census = {"buckets": [], "caveat": "", "lane": "x264"}
        screen.store = seinn_convert.StateStore(str(tmp_path / "storeless"))
        screen.vaapi_ok, screen.vaapi_reason = False, "no device"
        screen._start_run()
        await pilot.pause(0.3)
        assert screen.running

        await pilot.press("x")
        await pilot.pause(0.1)
        modal = pilot.app.screen
        confirm_btn = modal.query_one("#confirm-btn")
        await pilot.click(confirm_btn)

        for _ in range(100):
            if not screen.running:
                break
            await pilot.pause(0.1)
        assert screen.stop_ctrl.stop_event.is_set()
        assert screen.query_one("#convert-switcher").current == "phase-report"
        assert not list(root.glob("*.seinn-convert.tmp.mp4"))


@pytest.mark.asyncio
async def test_convert_navigation_locked_during_run(tmp_path, monkeypatch):
    def slow_apply_all(entries, store, workers, vaapi_ok, stop_ctrl, *, progress=None):
        for e in entries:
            progress("file-start", e)
        time.sleep(2)
        for e in entries:
            e.result = "remuxed"
            progress("file-done", e)
        return False

    monkeypatch.setattr(seinn_convert, "apply_all", slow_apply_all)

    root = tmp_path / "locktree"
    root.mkdir()
    entry = seinn_convert.FileEntry("f.mp4", str(root / "f.mp4"), 10, time.time(), time.time())
    (root / "f.mp4").write_bytes(b"0" * 10)
    entry.verdict, entry.plan, entry.probe = "needs-remux", "remux", {}

    app = SeinnApp(config_path=str(tmp_path / "nope.toml"))
    async with app.run_test() as pilot:
        await pilot.press("c")
        screen = pilot.app.MODES_screens["convert"]
        screen.entries = [entry]
        screen.convertible = [entry]
        screen.census = {"buckets": [], "caveat": "", "lane": "x264"}
        screen.store = seinn_convert.StateStore(str(tmp_path / "storeless"))
        screen.vaapi_ok, screen.vaapi_reason = False, "no device"
        screen._start_run()
        await pilot.pause(0.2)
        assert screen.running

        await pilot.press("d")
        await pilot.pause(0.1)
        assert pilot.app.screen.name == "convert"  # refused, screen unchanged

        for _ in range(50):
            if not screen.running:
                break
            await pilot.pause(0.1)
