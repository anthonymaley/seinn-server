#!/usr/bin/env python3
"""seinn TUI — Textual app for setup and conversion.

One deliverable wearing the seinn identity (warm greyscale, three semantic
colours, the four-stroke ramp mark): a Dashboard (the doctor's checks made
ambient), a Shares editor (the one and only writer of the config TOML), and
a Convert wizard (census -> confirm -> apply, driving seinn_convert.py by
import). This file imports seinn_agent.py and seinn_convert.py and drives
them — it contains zero probe/classify/census/doctor logic of its own.

Textual is the only new dependency, and it is optional: the import guard
below is the "gracefully absent" contract — a stranger who runs this file
without the venv gets told exactly what's missing and that nothing else
needs it (the agent and seinn-convert stay dependency-free).
"""

import sys

try:
    from textual.app import App
    from textual.containers import Container, Horizontal, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Input, Static
    from rich.text import Text
except ImportError:
    sys.stderr.write(
        "seinn: the TUI needs Textual, which is not installed.\n"
        "Install it:  sudo ./install.sh --with-tui   (or: pip install textual)\n"
        "Everything else works without it — the agent and seinn-convert "
        "are dependency-free.\n")
    sys.exit(1)

import argparse
import contextlib
import difflib
import getpass
import io
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request

# sys.path bootstrap: import the sibling engines whether this file is run
# from the repo checkout or from <prefix>/ after install.sh copies all three
# .py files side by side (server/install.sh, Step 7).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seinn_agent
import seinn_convert

TUI_VERSION = "0.1.0"


# ---------------------------------------------------------------------
# Design tokens (docs/design/README.md SS Design tokens, SS Brand).
# Terminal cells can't do the design's 8-14px radii; `round` box-drawing is
# the honest terminal equivalent for the panel corner treatment below — do
# not "fix" that into a sharper border, it is the intended degradation.
# ---------------------------------------------------------------------

INK = "#0A0A0B"
SURFACE = "#141416"
RAISED = "#1C1C1F"
LINE = "#28282C"
TERTIARY = "#6E6E73"
SECONDARY = "#A1A1A6"
PAPER = "#F5F5F3"
GREEN = "#34C759"    # healthy / finished
RED = "#FF3B30"       # failed / broken
ORANGE = "#FF9500"    # needs attention / not yet done
DESTRUCTIVE = "#FF453A"  # delete/remove affordances only

LEVEL_COLOR = {"PASS": GREEN, "FAIL": RED, "WARN": ORANGE, "INFO": TERTIARY, "SKIP": TERTIARY}

SEINN_CSS = f"""
Screen {{
    background: {INK};
    color: {PAPER};
}}
SeinnHeader {{
    background: {INK};
    color: {PAPER};
    height: 1;
    padding: 0 2;
}}
Footer {{
    background: {INK};
}}
DataTable {{
    background: {SURFACE};
    border: round {LINE};
}}
.panel {{
    background: {SURFACE};
    border: round {LINE};
    padding: 1 2;
    margin: 0 0 1 0;
}}
.panel-title {{
    color: {SECONDARY};
    text-style: bold;
    margin: 1 0 0 0;
}}
.ok {{ color: {GREEN}; }}
.fail {{ color: {RED}; }}
.warn {{ color: {ORANGE}; }}
.muted {{ color: {TERTIARY}; }}
.destructive {{ color: {DESTRUCTIVE}; }}
ModalScreen {{
    align: center middle;
}}
#modal-body {{
    background: {RAISED};
    border: round {LINE};
    padding: 1 2;
    width: 70%;
    height: auto;
}}
Button.-destructive {{
    background: {DESTRUCTIVE};
}}
"""


def _mark_glyphs():
    """The ramp mark's cell-grid construction, eighth-block glyphs climbing
    left to right (docs/design SS Brand). Falls back to ASCII when the
    terminal encoding is not UTF-8 rather than emitting mojibake."""
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    if "utf" in enc:
        return "▂▄▆█"  # ▂▄▆█
    return ".:|#"


class SeinnHeader(Static):
    """Left-aligned identity strip: mark + name + versions. Name is always
    'Seinn' — initial capital, never all-caps (docs/design SS Brand)."""

    def __init__(self, agent_version, tui_version):
        self.render_text = (
            f"{_mark_glyphs()} Seinn   agent v{agent_version} · tui v{tui_version}")
        super().__init__(Text(self.render_text, style=PAPER))


class SeinnScreen(Screen):
    """Base for the three mode screens: header + screen content + footer.
    Every screen carries the same identity strip and the same global
    keybinding hints — house style: comments state constraints, not
    narration."""

    def compose_content(self):
        raise NotImplementedError

    def compose(self):
        yield SeinnHeader(seinn_agent.AGENT_VERSION, TUI_VERSION)
        yield from self.compose_content()
        yield Footer()


class ConfirmModal(ModalScreen):
    """Yes/no confirmation. Destructive actions get the destructive-red
    button per docs/design (delete/remove affordances only)."""

    def __init__(self, message, destructive=False, confirm_label="Confirm"):
        super().__init__()
        self.message = message
        self.destructive = destructive
        self.confirm_label = confirm_label

    def compose(self):
        yield Vertical(
            Static(self.message),
            Horizontal(
                Button(self.confirm_label, id="confirm-btn",
                       classes="destructive" if self.destructive else ""),
                Button("Cancel", id="cancel-btn"),
            ),
            id="modal-body",
        )

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm-btn")


class MessageModal(ModalScreen):
    """One-button dismissal for read-only informational text (the sudo
    command, a failed-save message, etc.) — monospace, selectable."""

    def __init__(self, title, body, mono_body=None):
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.mono_body = mono_body

    def compose(self):
        widgets = [Static(self.title_text), Static(self.body_text)]
        if self.mono_body:
            widgets.append(Static(self.mono_body, classes="muted"))
        widgets.append(Button("OK", id="ok-btn"))
        yield Vertical(*widgets, id="modal-body")

    def on_button_pressed(self, event):
        self.dismiss(True)


# ---------------------------------------------------------------------
# Step 4 — Dashboard
# ---------------------------------------------------------------------

class DashboardScreen(SeinnScreen):
    """The doctor's nine checks as a live status panel, agent service
    state, and share facts — --doctor made ambient. All loading happens off
    the UI thread (Textual workers); the UI never freezes on a probe."""

    BINDINGS = [("r", "refresh_dashboard", "Refresh")]

    def __init__(self, config_path, service_name, **kwargs):
        super().__init__(**kwargs)
        self.config_path = config_path
        self.service_name = service_name
        self.checks = []

    def compose_content(self):
        yield Vertical(
            Static("Doctor", classes="panel-title"),
            DataTable(id="doctor-table", cursor_type="row"),
            Static("select a row and press enter to reveal its fix",
                   id="fix-panel", classes="panel muted"),
            Static("", id="verdict"),
            Static("Service", classes="panel-title"),
            Static("checking…", id="service-panel", classes="panel muted"),
            Static("Shares", classes="panel-title"),
            Static("checking…", id="shares-panel", classes="panel muted"),
            id="dashboard-root",
        )

    def on_mount(self):
        table = self.query_one("#doctor-table", DataTable)
        table.add_columns(("level", "level"), ("message", "message"))
        self.refresh_all()

    def action_refresh_dashboard(self):
        self.query_one("#verdict", Static).update(Text("re-running…", style=TERTIARY))
        self.refresh_all()

    def refresh_all(self):
        self.run_worker(self._load_doctor, thread=True, exclusive=True, group="doctor")
        self.run_worker(self._load_service, thread=True, exclusive=True, group="service")
        self.run_worker(self._load_shares, thread=True, exclusive=True, group="shares")

    # -- doctor panel -----------------------------------------------------

    def _load_doctor(self):
        results = list(seinn_agent.doctor_checks(self.config_path, self.service_name))
        self.app.call_from_thread(self._populate_doctor, results)

    def _populate_doctor(self, results):
        self.checks = results
        table = self.query_one("#doctor-table", DataTable)
        table.clear()
        for r in results:
            color = LEVEL_COLOR.get(r.level, TERTIARY)
            table.add_row(Text(r.level, style=color), Text(r.message, style=PAPER))
        code, verdict_line = seinn_agent.doctor_verdict(results)
        self.query_one("#verdict", Static).update(
            Text(verdict_line, style=GREEN if code == 0 else RED))
        self.query_one("#fix-panel", Static).update(
            "select a row and press enter to reveal its fix")

    def on_data_table_row_selected(self, event):
        if event.data_table.id != "doctor-table":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self.checks):
            r = self.checks[idx]
            text = r.fix if r.fix else "no action needed"
            self.query_one("#fix-panel", Static).update(Text(text, style=PAPER))

    # -- service panel ------------------------------------------------

    def _load_service(self):
        if shutil.which("systemctl") is None:
            self.app.call_from_thread(
                self._populate_service,
                "no systemd — fine for --no-service installs", None)
            return
        active = seinn_agent._doctor_systemd_active(self.service_name)
        since = None
        try:
            res = subprocess.run(
                ["systemctl", "show", "-p", "ActiveEnterTimestamp", self.service_name],
                capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if line.startswith("ActiveEnterTimestamp="):
                    since = line.split("=", 1)[1].strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
        text = f"{self.service_name}: {'active' if active else 'inactive'}"
        if since:
            text += f" (since {since})"
        self.app.call_from_thread(self._populate_service, text, active)

    def _populate_service(self, text, ok):
        color = TERTIARY if ok is None else (GREEN if ok else ORANGE)
        self.query_one("#service-panel", Static).update(Text(text, style=color))

    # -- shares facts panel -------------------------------------------

    def _load_shares(self):
        try:
            data, _ = config_load_raw(self.config_path)
        except (OSError, tomllib.TOMLDecodeError):
            self.app.call_from_thread(self._populate_shares_error)
            return
        roots = data.get("roots", {})
        port = data.get("port", 8378)
        agent_ok = True
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/roots", timeout=2) as resp:
                resp.read()
        except Exception:
            agent_ok = False
        rows = []
        for name, path in sorted(roots.items()):
            if not isinstance(path, str):
                continue
            free = "—"
            try:
                st = os.statvfs(path)
                free = seinn_convert.fmt_bytes(st.f_bavail * st.f_frsize)
            except OSError:
                pass
            count = "—"
            if agent_ok:
                try:
                    q = urllib.parse.quote(name)
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/list?root={q}", timeout=2) as resp:
                        import json as _json
                        payload = _json.loads(resp.read())
                    count = str(len(payload.get("entries", [])))
                except Exception:
                    count = "—"
            rows.append((name, path, count, free))
        self.app.call_from_thread(self._populate_shares, rows, agent_ok, port)

    def _populate_shares(self, rows, agent_ok, port):
        panel = self.query_one("#shares-panel", Static)
        if not agent_ok:
            panel.update(Text(f"agent not answering on 127.0.0.1:{port}", style=ORANGE))
            return
        if not rows:
            panel.update(Text("no roots configured", style=TERTIARY))
            return
        lines = [f"{n:<12} {p:<40} {c:>6} entries  {f} free" for n, p, c, f in rows]
        panel.update(Text("\n".join(lines), style=PAPER))

    def _populate_shares_error(self):
        self.query_one("#shares-panel", Static).update(
            Text("config unreadable — see doctor panel above", style=ORANGE))


# ---------------------------------------------------------------------
# Step 5 — Shares screen + the safe TOML writer
# ---------------------------------------------------------------------
#
# The Shares screen is the only writer in the whole TUI, and the write is a
# full-file regeneration from a template — never string-append, never
# partial edit. The writer emits every top-level key first and [roots] last,
# always, by construction: the below-[roots] trap (a stray top-level key
# appended after the table becoming a served share) is structurally
# impossible here, not merely avoided.

class ConfigNotSerializable(Exception):
    """Raised naming the offending top-level key. tomllib reads but does
    not write; this writer refuses to save any config it cannot round-trip
    losslessly rather than guess."""


_KNOWN_KEY_ORDER = [
    "port", "bind", "delete_enabled", "hide_dotfiles", "thumbs_enabled",
    "cache_dir", "auth_token", "state_db",
]

_ROOT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def config_load_raw(path):
    """-> (data, raw_text). tomllib.load for the data, the raw text
    verbatim for the diff preview. Raises OSError/TOMLDecodeError on a
    broken file — the caller (Shares screen) goes read-only and points at
    the Dashboard's doctor finding rather than "fixing" it by regenerating."""
    with open(path, "rb") as f:
        raw = f.read()
    data = tomllib.loads(raw.decode("utf-8"))
    return data, raw.decode("utf-8")


def _encode_toml_string(s):
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_toml_value(key, value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _encode_toml_string(value)
    raise ConfigNotSerializable(key)


def config_serialize(data):
    """Explicit serializer for exactly this schema: top-level scalars + one
    [roots] table of string paths. [roots] last, always — the structural
    guarantee the below-[roots] trap needs (bit twice, 2026-07-30)."""
    top = {k: v for k, v in data.items() if k != "roots"}
    roots = data.get("roots", {})
    unknown = sorted(k for k in top if k not in _KNOWN_KEY_ORDER)
    order = [k for k in _KNOWN_KEY_ORDER if k in top] + unknown

    lines = []
    for key in order:
        encoded = _encode_toml_value(key, top[key])
        if key == "auth_token":
            lines.append("")
            lines.append("# Required on state-changing routes (DELETE, progress-save). Reads stay open")
            lines.append("# on the LAN. Give it to the app once when adding this server.")
        if key == "state_db":
            lines.append("")
            lines.append("# Watched-state database (sqlite, created on first write).")
        lines.append(f"{key} = {encoded}")

    lines.append("")
    lines.append("# EVERY top-level key must sit ABOVE [roots]. Appended below, TOML reads it as")
    lines.append("# a share — which is how the auth token briefly became a publicly-listed root.")
    lines.append("[roots]")
    for name, path in roots.items():
        if not _ROOT_NAME_RE.match(name):
            raise ConfigNotSerializable(f"roots.{name} (invalid root name)")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ConfigNotSerializable(f"roots.{name} (not an absolute path string)")
        lines.append(f"{name} = {_encode_toml_string(path)}")

    return "\n".join(lines) + "\n"


def config_save(path, data):
    """Atomic, permission-preserving write:
      1. serialize FIRST (may raise ConfigNotSerializable — before any I/O,
         so a bad in-memory edit never touches disk);
      2. .bak of the prior bytes, mode copied;
      3. tempfile in the same directory, fchmod to the original mode,
         fchown to the original owner when running as root, os.replace;
      4. real read-back verify (tomllib.load the new file, compare roots)
         — a bad write is detected, not trusted.
    A failed write leaves the original untouched. Returns the .bak path, or
    None when the file was created fresh (nothing to back up)."""
    serialized = config_serialize(data)
    dirpath = os.path.dirname(os.path.abspath(path)) or "."
    exists = os.path.exists(path)

    bak_path = None
    if exists:
        st = os.stat(path)
        mode = st.st_mode & 0o777
        uid, gid = st.st_uid, st.st_gid
        with open(path, "rb") as f:
            prior_raw = f.read()
        bak_path = path + ".bak"
        with open(bak_path, "wb") as f:
            f.write(prior_raw)
        os.chmod(bak_path, mode)
    else:
        mode = 0o600
        uid, gid = os.getuid(), os.getgid()

    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=".seinn-config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(serialized)
        os.chmod(tmp_path, mode)
        if os.geteuid() == 0:
            try:
                os.chown(tmp_path, uid, gid)
            except OSError:
                pass  # non-root-writable-via-ACL case: warn, not fail (spec)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

    with open(path, "rb") as f:
        reloaded = tomllib.load(f)
    if reloaded.get("roots", {}) != data.get("roots", {}):
        raise RuntimeError(f"read-back verify failed: {path} does not match what was saved")
    return bak_path


DEFAULT_NEW_CONFIG = {
    "port": 8378, "bind": "0.0.0.0", "delete_enabled": False,
    "hide_dotfiles": True, "thumbs_enabled": True,
    "cache_dir": "/var/tmp/seinn-thumbs", "auth_token": "",
    "state_db": "/opt/seinn/progress.db", "roots": {},
}


class RootEditModal(ModalScreen):
    """Add/edit modal: name + path Inputs with as-you-type validation
    (debounced 300 ms). Each probe result is its own line: green pass, red
    with the doctor's fix text when unreadable, orange (not red) when only
    unwritable — write only matters for delete, and the line says so."""

    def __init__(self, name="", path="", editing=False):
        super().__init__()
        self.orig_name = name
        self.editing = editing
        self._debounce_timer = None
        self._valid = False

    def compose(self):
        yield Vertical(
            Static("Edit share" if self.editing else "Add share"),
            Input(value=self.orig_name, placeholder="name", id="name-input"),
            Static("", id="name-check", classes="muted"),
            Input(placeholder="/absolute/path", id="path-input"),
            Static("", id="path-check", classes="muted"),
            Horizontal(
                Button("Save", id="save-btn", disabled=True),
                Button("Cancel", id="cancel-btn"),
            ),
            id="modal-body",
        )

    def on_input_changed(self, event):
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(0.3, self._validate)

    def _validate(self):
        name = self.query_one("#name-input", Input).value.strip()
        path = self.query_one("#path-input", Input).value.strip()

        name_ok = bool(_ROOT_NAME_RE.match(name))
        self.query_one("#name-check", Static).update(
            Text("ok" if name_ok else "invalid name (use A-Za-z0-9_-)",
                 style=GREEN if name_ok else RED))

        path_lines = []
        path_ok = True
        if not path:
            path_ok = False
        elif not os.path.isabs(path):
            path_lines.append((RED, "path must be absolute"))
            path_ok = False
        elif not os.path.exists(path):
            path_lines.append((RED, "path does not exist"))
            path_ok = False
        elif not os.path.isdir(path):
            path_lines.append((RED, "path is not a directory"))
            path_ok = False
        else:
            readable, probe_file, _err = seinn_agent._doctor_probe_readable(path)
            if not readable:
                fix = seinn_agent._doctor_fix_unreadable(path, getpass.getuser())
                path_lines.append((RED, f"not readable — fix: {fix}"))
                path_ok = False
            else:
                path_lines.append((GREEN, "readable"))
                writable, _werr = seinn_agent._doctor_probe_writable(path, probe_file)
                if writable:
                    path_lines.append((GREEN, "writable"))
                else:
                    path_lines.append(
                        (ORANGE, "not writable — delete-from-the-couch needs "
                                 "write; playback doesn't"))

        check = self.query_one("#path-check", Static)
        if path_lines:
            text = Text()
            for i, (color, msg) in enumerate(path_lines):
                if i:
                    text.append("\n")
                text.append(msg, style=color)
            check.update(text)
        else:
            check.update("")

        self._valid = name_ok and path_ok
        self.query_one("#save-btn", Button).disabled = not self._valid

    def on_button_pressed(self, event):
        if event.button.id == "save-btn":
            if self._valid:
                name = self.query_one("#name-input", Input).value.strip()
                path = self.query_one("#path-input", Input).value.strip()
                self.dismiss((name, path))
        else:
            self.dismiss(None)


class SaveDiffModal(ModalScreen):
    """Unified diff of current file -> serialized result, shown before any
    write happens — comment loss from regeneration is made visible instead
    of silent."""

    def __init__(self, diff_text):
        super().__init__()
        self.diff_text = diff_text or "(no textual changes)"

    def compose(self):
        yield Vertical(
            Static("Save — review the diff"),
            Static(self.diff_text, id="diff-body", classes="muted"),
            Horizontal(
                Button("Write", id="confirm-btn"),
                Button("Cancel", id="cancel-btn"),
            ),
            id="modal-body",
        )

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm-btn")


class RestartOfferModal(ModalScreen):
    """The only restart offer in the TUI (single writer, single restarter).
    Never sudos on the user's behalf — shows the exact command instead."""

    def __init__(self, service_name):
        super().__init__()
        self.service_name = service_name
        self.can_restart = os.geteuid() == 0 and shutil.which("systemctl") is not None

    def compose(self):
        if self.can_restart:
            yield Vertical(
                Static(f"the agent reads config at startup — restart "
                       f"{self.service_name} now?"),
                Horizontal(Button("Yes", id="yes-btn"), Button("No", id="no-btn")),
                id="modal-body",
            )
        else:
            user = getpass.getuser()
            yield Vertical(
                Static(f"the agent reads config at startup — restart "
                       f"{self.service_name} now?"),
                Static(f"sudo systemctl restart {self.service_name}", classes="muted"),
                Static(f"the TUI is running as {user} and won't sudo for you"),
                Button("OK", id="no-btn"),
                id="modal-body",
            )

    def on_button_pressed(self, event):
        if event.button.id == "yes-btn":
            try:
                res = subprocess.run(
                    ["systemctl", "restart", self.service_name],
                    capture_output=True, text=True, timeout=15)
                self.app.notify(
                    f"systemctl restart {self.service_name}: exit {res.returncode}",
                    severity="information" if res.returncode == 0 else "error")
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.app.notify(f"restart failed: {exc}", severity="error")
        self.dismiss(None)


class SharesScreen(SeinnScreen):
    """List/add/edit/remove roots in the TOML. The one and only screen that
    writes config."""

    BINDINGS = [
        ("a", "add_root", "Add"),
        ("e", "edit_root", "Edit"),
        ("x", "remove_root", "Remove"),
        ("w", "write_config", "Save"),
    ]

    def __init__(self, config_path, service_name, **kwargs):
        super().__init__(**kwargs)
        self.config_path = config_path
        self.service_name = service_name
        self.data = None
        self.raw_text = None
        self.read_only = False
        self.config_exists = True

    def compose_content(self):
        yield Vertical(
            Static("", id="ro-banner"),
            DataTable(id="roots-table", cursor_type="row"),
            id="shares-root",
        )

    def on_mount(self):
        table = self.query_one("#roots-table", DataTable)
        table.add_columns("name", "path")
        self.reload()

    def reload(self):
        self.run_worker(self._load, thread=True, exclusive=True)

    def _load(self):
        exists = os.path.exists(self.config_path)
        writable = True
        owner_info = None
        if exists:
            try:
                with open(self.config_path, "r+b"):
                    pass
            except OSError:
                writable = False
                try:
                    import pwd
                    st = os.stat(self.config_path)
                    owner_info = (pwd.getpwuid(st.st_uid).pw_name, st.st_uid)
                except Exception:
                    owner_info = ("unknown", None)
        data = raw = parse_error = None
        if exists:
            try:
                data, raw = config_load_raw(self.config_path)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                parse_error = str(exc)
        self.app.call_from_thread(
            self._populate, exists, writable, owner_info, data, raw, parse_error)

    def _populate(self, exists, writable, owner_info, data, raw, parse_error):
        banner = self.query_one("#ro-banner", Static)
        self.config_exists = exists

        if parse_error:
            self.read_only = True
            self.data = None
            banner.update(Text(
                f"config unreadable — see Dashboard's doctor finding: "
                f"{parse_error}", style=ORANGE))
            self._refresh_table()
            return

        if not exists:
            self.read_only = False
            self.data = dict(DEFAULT_NEW_CONFIG)
            self.data["roots"] = {}
            self.data["auth_token"] = secrets.token_urlsafe(32)
            self.raw_text = ""
            banner.update(Text(
                "config missing — press w to create it with install "
                "defaults", style=ORANGE))
            self._refresh_table()
            return

        self.data = data
        self.raw_text = raw
        if not writable:
            self.read_only = True
            owner, uid = owner_info
            banner.update(Text(
                f"config owned by {owner} (uid {uid}); this session runs as "
                f"{getpass.getuser()}. To edit shares: sudo seinn", style=ORANGE))
        else:
            self.read_only = False
            banner.update("")
        self._refresh_table()

    def _refresh_table(self):
        table = self.query_one("#roots-table", DataTable)
        table.clear()
        if self.data is None:
            return
        for name, path in sorted(self.data.get("roots", {}).items()):
            table.add_row(name, Text(str(path), style=TERTIARY))

    def _refuse_if_read_only(self):
        if self.read_only:
            self.app.notify(
                "read-only — sudo seinn to edit shares", severity="warning")
            return True
        return False

    def _selected_root(self):
        table = self.query_one("#roots-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return None, None
        row = table.get_row_at(table.cursor_row)
        return str(row[0]), str(row[1])

    def action_add_root(self):
        if self._refuse_if_read_only():
            return
        self.app.push_screen(RootEditModal(), self._on_add_result)

    def _on_add_result(self, result):
        if result is None or self.data is None:
            return
        name, path = result
        self.data.setdefault("roots", {})[name] = path
        self._refresh_table()

    def action_edit_root(self):
        if self._refuse_if_read_only():
            return
        name, path = self._selected_root()
        if name is None:
            return
        self.app.push_screen(
            RootEditModal(name=name, path=path, editing=True),
            lambda result: self._on_edit_result(name, result))

    def _on_edit_result(self, orig_name, result):
        if result is None or self.data is None:
            return
        new_name, new_path = result
        roots = self.data.setdefault("roots", {})
        if orig_name in roots and orig_name != new_name:
            del roots[orig_name]
        roots[new_name] = new_path
        self._refresh_table()

    def action_remove_root(self):
        if self._refuse_if_read_only():
            return
        name, path = self._selected_root()
        if name is None:
            return
        self.app.push_screen(
            ConfirmModal(
                f"Remove share {name!r} ({path})? This removes the share "
                f"from the config — files on disk are untouched.",
                destructive=True, confirm_label="Remove"),
            lambda confirmed: self._on_remove_result(name, confirmed))

    def _on_remove_result(self, name, confirmed):
        if not confirmed or self.data is None:
            return
        self.data.get("roots", {}).pop(name, None)
        self._refresh_table()

    def action_write_config(self):
        if self._refuse_if_read_only():
            return
        if self.data is None:
            return
        try:
            new_text = config_serialize(self.data)
        except ConfigNotSerializable as exc:
            self.app.notify(f"cannot save: {exc}", severity="error")
            return
        old_text = self.raw_text or ""
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=self.config_path, tofile=f"{self.config_path} (new)"))
        self.app.push_screen(SaveDiffModal(diff), self._on_save_confirmed)

    def _on_save_confirmed(self, confirmed):
        if not confirmed:
            return
        self.run_worker(self._do_save, thread=True, exclusive=True, group="save")

    def _do_save(self):
        try:
            bak_path = config_save(self.config_path, self.data)
            self.app.call_from_thread(self._after_save, True, bak_path, None)
        except Exception as exc:
            self.app.call_from_thread(self._after_save, False, None, str(exc))

    def _after_save(self, ok, bak_path, error):
        if ok:
            msg = f"saved — backup at {bak_path}" if bak_path else "saved (new file created)"
            self.app.notify(msg)
            self.reload()
            self.app.push_screen(RestartOfferModal(self.service_name))
        else:
            self.app.notify(f"save failed: {error}", severity="error")


# ---------------------------------------------------------------------
# Step 6 — Convert screen
# ---------------------------------------------------------------------

class ConvertScreen(SeinnScreen):
    """Pick -> census -> confirm -> run -> report, driving seinn_convert by
    import. Exactly one census-or-run worker alive at a time; navigation
    away mid-run is refused at the App level (action_switch_mode)."""

    BINDINGS = [
        ("escape", "escape_phase", "Back"),
        ("enter", "advance_phase", "Continue"),
        ("x", "cancel_or_stop", "Cancel/Stop"),
        ("l", "toggle_log", "Log"),
    ]

    def __init__(self, config_path, service_name, **kwargs):
        super().__init__(**kwargs)
        self.config_path = config_path
        self.service_name = service_name
        self.root_path = None
        self.entries = []
        self.census = None
        self.vaapi_ok = None
        self.vaapi_reason = None
        self.stale_temps = []
        self.store = None
        self.stop_ctrl = None
        self.convertible = []
        self.running = False
        self.interrupted = False
        self.elapsed = 0.0
        self._run_start = 0.0
        self._done_bytes = 0
        self._total_bytes = 0
        self._log_lines = []

    def compose_content(self):
        yield ContentSwitcher(
            Container(
                Static("Pick a share, or type a path and press enter"),
                DataTable(id="pick-table", cursor_type="row"),
                Input(placeholder="/absolute/path", id="pick-input"),
                id="phase-pick",
            ),
            Container(
                Static("", id="census-status"),
                DataTable(id="census-table"),
                Static("", id="census-caveat", classes="muted"),
                Static("press enter to continue, esc to go back",
                       classes="muted"),
                id="phase-census",
            ),
            Container(
                Static("", id="confirm-text"),
                Horizontal(
                    Button("Convert", id="convert-btn"),
                    Button("Cancel", id="cancel-btn"),
                ),
                id="phase-confirm",
            ),
            Container(
                DataTable(id="run-table", cursor_type="row"),
                Static("", id="run-bar"),
                Static("", id="run-log", classes="muted"),
                id="phase-run",
            ),
            Container(
                Static("", id="report-text"),
                Static("press enter to pick another share", classes="muted"),
                id="phase-report",
            ),
            initial="phase-pick",
            id="convert-switcher",
        )

    def on_mount(self):
        self.query_one("#run-log", Static).display = False
        # #report-text is the only Static that ever needs to hold key focus
        # (report phase has no other focusable widget); made focusable here
        # rather than left to grab default DOM-order focus like #pick-input
        # once did (a focused Input silently eats every keystroke as text).
        self.query_one("#report-text", Static).can_focus = True
        self._load_pick_table()
        self.query_one("#pick-table", DataTable).focus()

    # -- phase: pick ----------------------------------------------------

    def _load_pick_table(self):
        table = self.query_one("#pick-table", DataTable)
        table.clear(columns=True)
        table.add_columns("name", "path", "free")
        try:
            data, _ = config_load_raw(self.config_path)
            roots = data.get("roots", {})
        except (OSError, tomllib.TOMLDecodeError):
            roots = {}
        for name, path in sorted(roots.items()):
            free = "—"
            try:
                st = os.statvfs(path)
                free = seinn_convert.fmt_bytes(st.f_bavail * st.f_frsize)
            except OSError:
                pass
            table.add_row(name, path, free)

    def on_data_table_row_selected(self, event):
        if event.data_table.id != "pick-table":
            return
        if self.query_one(ContentSwitcher).current != "phase-pick":
            return
        row = event.data_table.get_row_at(event.cursor_row)
        self._start_census(str(row[1]))

    def on_input_submitted(self, event):
        if event.input.id != "pick-input":
            return
        path = event.value.strip()
        if os.path.isabs(path) and os.path.isdir(path):
            self._start_census(path)
        else:
            self.app.notify(
                "path must be an absolute, existing directory", severity="error")

    # -- phase: census ----------------------------------------------------

    def _start_census(self, root_path):
        self.root_path = root_path
        self.query_one(ContentSwitcher).current = "phase-census"
        # ContentSwitcher hides the other phases, it doesn't unmount them —
        # a stale focus left on an Input in a hidden phase would silently
        # eat every keystroke as text instead of dispatching bindings
        # (measured: "x" pressed while pick-input had focus never reached
        # action_cancel_or_stop). Every phase switch below re-homes focus.
        self.query_one("#census-table", DataTable).focus()
        self.query_one("#census-status", Static).update("probing… 0/0")
        self.query_one("#census-caveat", Static).update("")
        self.run_worker(self._census_worker, thread=True, exclusive=True, group="census")

    def _census_worker(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            entries, stale_temps = seinn_convert.discover(self.root_path, 600, force=False)
            store = seinn_convert.StateStore(self.root_path)
            total = sum(1 for e in entries if e.verdict is None)
            probed = cached = 0
            for e in entries:
                if e.verdict is not None:
                    continue
                fresh = seinn_convert.classify_with_cache(e, store, retry_failed=False)
                if fresh:
                    probed += 1
                else:
                    cached += 1
                self.app.call_from_thread(self._update_probe_progress, probed + cached, total)
            store.flush_now()
            vaapi_ok, vaapi_reason = seinn_convert.vaapi_available()
            remux_audio_count = sum(1 for e in entries if e.plan == "remux-audio")
            data = seinn_convert.census_data(
                entries, self.root_path, 10, probed, cached, vaapi_ok, remux_audio_count)
        self._log(buf.getvalue())
        self.store = store
        self.entries = entries
        self.census = data
        self.vaapi_ok, self.vaapi_reason = vaapi_ok, vaapi_reason
        self.stale_temps = stale_temps
        self.app.call_from_thread(self._populate_census)

    def _update_probe_progress(self, n, total):
        self.query_one("#census-status", Static).update(f"probing… {n}/{total}")

    def _populate_census(self):
        table = self.query_one("#census-table", DataTable)
        table.clear(columns=True)
        table.add_columns("bucket", "count", "size", "estimate", "note")
        for b in self.census["buckets"]:
            est = (seinn_convert.fmt_duration(b["est_seconds"])
                   if b["est_seconds"] is not None else "—")
            table.add_row(b["name"], str(b["count"]), seinn_convert.fmt_bytes(b["bytes"]),
                          est, b["note"])
        self.query_one("#census-status", Static).update(
            f"lane: {self.census['lane']} ({self.vaapi_reason})")
        caveat = self.census["caveat"]
        if self.stale_temps:
            caveat = (f"{len(self.stale_temps)} stale temp file(s) from a previous "
                      f"interrupted run found (not converted, cleanup only)\n" + caveat)
        self.query_one("#census-caveat", Static).update(Text(caveat, style=TERTIARY))
        self.convertible = [e for e in self.entries
                            if e.plan in ("remux", "remux-audio", "transcode")]

    def action_escape_phase(self):
        switcher = self.query_one(ContentSwitcher)
        if switcher.current == "phase-census":
            switcher.current = "phase-pick"
            self.query_one("#pick-table", DataTable).focus()

    def action_advance_phase(self):
        switcher = self.query_one(ContentSwitcher)
        if switcher.current == "phase-census":
            if not self.convertible:
                self.app.notify("nothing to convert.", severity="warning")
                return
            self._populate_confirm()
            switcher.current = "phase-confirm"
        elif switcher.current == "phase-report":
            switcher.current = "phase-pick"
            self._load_pick_table()
            self.query_one("#pick-table", DataTable).focus()

    # -- phase: confirm ---------------------------------------------------

    def _populate_confirm(self):
        by_verdict = {}
        for e in self.entries:
            by_verdict.setdefault(e.verdict, []).append(e)
        remux_entries = by_verdict.get("needs-remux", [])
        remux_n = len(remux_entries)
        audio_n = sum(1 for e in remux_entries if e.plan == "remux-audio")
        trans_n = len(by_verdict.get("needs-transcode", []))
        total_bytes = sum(e.size for e in self.convertible)
        est = sum(
            (b["est_seconds"] or 0) for b in self.census["buckets"]
            if b["name"] in ("needs-remux", "needs-transcode"))
        text = (
            f"{remux_n} file(s) to remux ({audio_n} audio-only), {trans_n} to "
            f"transcode, {seinn_convert.fmt_bytes(total_bytes)} total, "
            f"~{seinn_convert.fmt_duration(est)} estimated, {self.census['lane']} lane.\n\n"
            f"sources are replaced in place after verification; optimal files "
            f"are never touched; Ctrl-C-safe (verify-then-atomic).")
        self.query_one("#confirm-text", Static).update(text)
        self.query_one("#convert-btn", Button).disabled = not self.convertible
        self.query_one("#cancel-btn", Button).focus()  # default focus NO

    def on_button_pressed(self, event):
        if event.button.id == "convert-btn":
            self._start_run()
        elif event.button.id == "cancel-btn":
            self.query_one(ContentSwitcher).current = "phase-census"
            self.query_one("#census-table", DataTable).focus()

    # -- phase: run ---------------------------------------------------

    def _start_run(self):
        self.query_one(ContentSwitcher).current = "phase-run"
        self.running = True
        table = self.query_one("#run-table", DataTable)
        table.clear(columns=True)
        table.add_columns("file", ("lane", "lane"), ("state", "state"))
        for e in self.convertible:
            table.add_row(e.relpath, e.plan or "?", "waiting", key=e.relpath)
        table.focus()
        self.stop_ctrl = seinn_convert.StopController()
        self._run_start = time.time()
        self._done_bytes = 0
        self._total_bytes = sum(e.size for e in self.convertible)
        self.query_one("#run-bar", Static).update("0%  elapsed 0 s")
        self.run_worker(self._run_worker, thread=True, exclusive=True, group="run")

    def _on_progress(self, event, entry):
        try:
            self.app.call_from_thread(
                self._apply_progress, event, entry.relpath, entry.result,
                entry.detail, entry.size)
        except Exception:
            pass  # a broken observer must never break a conversion (Step 2)

    def _apply_progress(self, event, relpath, result, detail, size):
        table = self.query_one("#run-table", DataTable)
        if event == "file-start":
            table.update_cell(relpath, "state", "running")
        else:
            state = result or "done"
            color = RED if state.startswith("FAILED") else PAPER
            table.update_cell(relpath, "state", Text(state, style=color))
            if result and not result.startswith("FAILED"):
                self._done_bytes += size
            self._update_bar()

    def _update_bar(self):
        elapsed = time.time() - self._run_start
        pct = (self._done_bytes / self._total_bytes * 100) if self._total_bytes else 100
        text = f"{pct:.0f}%  elapsed {seinn_convert.fmt_duration(elapsed)}"
        if self._done_bytes:
            rate = self._done_bytes / elapsed if elapsed else 0
            remaining = max(0, self._total_bytes - self._done_bytes)
            if rate:
                text += f"  remaining ~{seinn_convert.fmt_duration(remaining / rate)}"
        text += "  (per-file %% not reported — bar advances on file completion)"
        self.query_one("#run-bar", Static).update(text)

    def _run_worker(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            interrupted = seinn_convert.apply_all(
                self.convertible, self.store, 1, self.vaapi_ok, self.stop_ctrl,
                progress=self._on_progress)
        self._log(buf.getvalue())
        elapsed = time.time() - self._run_start
        self.app.call_from_thread(self._finish_run, interrupted, elapsed)

    def _finish_run(self, interrupted, elapsed):
        self.running = False
        self.interrupted = interrupted
        self.elapsed = elapsed
        self._populate_report()
        self.query_one(ContentSwitcher).current = "phase-report"
        self.query_one("#report-text", Static).focus()

    def action_cancel_or_stop(self):
        switcher = self.query_one(ContentSwitcher)
        if switcher.current != "phase-run" or not self.running or self.stop_ctrl is None:
            return
        self.app.push_screen(
            ConfirmModal(
                "stops after in-flight ffmpeg is terminated; state is saved; "
                "a re-run resumes", destructive=True, confirm_label="Stop"),
            self._on_cancel_confirmed)

    def _on_cancel_confirmed(self, confirmed):
        if confirmed and self.stop_ctrl is not None:
            self.run_worker(self.stop_ctrl.request_stop, thread=True)

    def action_toggle_log(self):
        log = self.query_one("#run-log", Static)
        log.display = not log.display
        if log.display:
            log.update(Text("\n".join(self._log_lines[-200:]), style=TERTIARY))

    def _log(self, text):
        if not text:
            return
        self._log_lines.extend(text.splitlines())

    # -- phase: report ----------------------------------------------------

    def _populate_report(self):
        converted = [e for e in self.convertible
                    if e.result and not e.result.startswith("FAILED")]
        failed = [e for e in self.convertible
                 if e.result and e.result.startswith("FAILED")]
        underfoot = [e for e in self.convertible if e.verdict == "changed-underfoot"]
        lines = []
        if self.interrupted:
            lines.append("interrupted — state saved, re-run to resume")
        lines.append(
            f"totals: {len(converted)} converted, {len(failed)} failed, "
            f"{len(underfoot)} changed-underfoot")
        lines.append(f"elapsed: {seinn_convert.fmt_duration(self.elapsed)}")
        for e in failed:
            lines.append(f"  {e.relpath}: {e.result} — {e.detail}")
        color = ORANGE if self.interrupted else PAPER
        self.query_one("#report-text", Static).update(Text("\n".join(lines), style=color))


# ---------------------------------------------------------------------
# Step 3 — shell, theme, launcher
# ---------------------------------------------------------------------

class SeinnApp(App):
    """The Textual shell: three screens as modes, global navigation, the
    seinn theme. Dashboard is the default mode."""

    CSS = SEINN_CSS
    TITLE = "Seinn"
    BINDINGS = [
        ("d", "switch_mode('dashboard')", "Dashboard"),
        ("s", "switch_mode('shares')", "Shares"),
        ("c", "switch_mode('convert')", "Convert"),
        ("q", "quit", "Quit"),
    ]

    DEFAULT_MODE = "dashboard"

    def __init__(self, config_path=None, service_name="seinn-agent"):
        self.config_path = config_path or seinn_agent.doctor_default_config_path()
        self.service_name = service_name
        # MODES must be set before App.__init__ (it copies MODES into
        # self._modes there) and must hold callables, not Screen instances
        # (Textual constructs — and reuses, once built — one per mode). A
        # closure that always returns the SAME instance gives Shares/Convert
        # their state-survives-a-switch behavior (unsaved edits, in-flight
        # census) without Textual complaining about a bare instance in MODES.
        self._dashboard_screen = DashboardScreen(
            self.config_path, self.service_name, name="dashboard")
        self._shares_screen = SharesScreen(
            self.config_path, self.service_name, name="shares")
        self._convert_screen = ConvertScreen(
            self.config_path, self.service_name, name="convert")
        self.MODES = {
            "dashboard": lambda: self._dashboard_screen,
            "shares": lambda: self._shares_screen,
            "convert": lambda: self._convert_screen,
        }
        super().__init__()

    @property
    def MODES_screens(self):
        """Convenience accessor the tests use: {mode name: screen instance}
        — MODES itself holds factory callables, not instances (Textual's
        contract), so this is the one place that resolves them."""
        return {
            "dashboard": self._dashboard_screen,
            "shares": self._shares_screen,
            "convert": self._convert_screen,
        }

    async def action_switch_mode(self, mode: str) -> None:
        # Navigation lock: a half-watched conversion is worse than a modal
        # refusal (Step 6). Central here so every nav path (keys, footer
        # clicks) goes through one gate.
        current = self.screen
        if isinstance(current, ConvertScreen) and current.running:
            self.notify("conversion running — cancel first", severity="warning")
            return
        await super().action_switch_mode(mode)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="seinn",
        description="seinn TUI — dashboard, shares editor, and convert wizard.")
    ap.add_argument("--config", default=None,
                    help="config TOML path (default: the install layout's path)")
    ap.add_argument("--service-name", default="seinn-agent",
                    help="systemd unit name for the Dashboard's service checks "
                         "(default seinn-agent)")
    ap.add_argument("--version", action="store_true", help="print versions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        # Dispatched before any App/Screen construction — argparse works
        # even on a dumb terminal that can't take a full Textual app.
        print(f"seinn-tui {TUI_VERSION} (agent {seinn_agent.AGENT_VERSION})")
        return 0
    config_path = args.config or seinn_agent.doctor_default_config_path()
    app = SeinnApp(config_path=config_path, service_name=args.service_name)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
