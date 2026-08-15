#!/usr/bin/env bash
# install.sh — one-command installer for seinn-agent.
#
# sudo ./install.sh [--prefix /opt/seinn] [--user seinn-or-existing-user]
#                   [--port 8378] [--root name=/abs/path]... [--service-name seinn-agent]
#                   [--no-service] [--force] [--dry-run] [--wizard] [--no-wizard]
#                   [--with-tui]
#
# Idempotent and re-runnable: always safe to run again. It replaces the
# agent .py, never the config (see the guard below), and only rewrites the
# systemd unit with --force.
#
# --with-tui is optional and additive: it creates a venv at <prefix>/venv,
# pip-installs Textual into it, and writes the /usr/local/bin/seinn
# launcher. Off by default, the base install stays zero-dependency. A
# failed venv/pip step degrades to "no TUI" and never fails the agent
# install (see step 6 below).
#
# No args on a TTY (or --wizard, even without one) walks you through an
# interactive wizard instead of requiring the flags above; --no-wizard
# suppresses it. See wizard() below.
set -euo pipefail

# TEST-ONLY: lets --dry-run print the other OS's transcript for diffing.
OS_UNAME="${SEINN_INSTALL_TEST_OS:-$(uname -s)}"
is_darwin() { [ "$OS_UNAME" = "Darwin" ]; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# argument count is the wizard's no-args-on-a-TTY trigger; capture it before
# the parse loop below consumes "$@".
ARGC="$#"

# ---- defaults ---------------------------------------------------------
PREFIX="/opt/seinn"
DEFAULT_PREFIX="$PREFIX"
INSTALL_USER=""
PORT="8378"
SERVICE_NAME="seinn-agent"
DEFAULT_SERVICE_NAME="$SERVICE_NAME"
SERVICE_NAME_GIVEN=0
PREFIX_GIVEN=0
NO_SERVICE=0
FORCE=0
DRY_RUN=0
WIZARD_FLAG=0
NO_WIZARD_FLAG=0
WITH_TUI=0
DELETE_ENABLED="false"
ROOTS=()

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--prefix /opt/seinn] [--user seinn-or-existing-user]
                          [--port 8378] [--root name=/abs/path]...
                          [--service-name seinn-agent]
                          [--no-service] [--force] [--dry-run]
                          [--wizard] [--no-wizard]
                          [--with-tui]

No flags on a TTY runs the interactive wizard. --wizard forces it (even
without a TTY, seeded from any other flags given); --no-wizard suppresses
it unconditionally.

--with-tui installs the optional TUI (venv + Textual + the seinn
launcher). Off by default; a failure here never fails the agent install.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            PREFIX_GIVEN=1
            shift 2
            ;;
        --user)
            INSTALL_USER="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --root)
            ROOTS+=("$2")
            shift 2
            ;;
        --service-name)
            SERVICE_NAME="$2"
            SERVICE_NAME_GIVEN=1
            shift 2
            ;;
        --no-service)
            NO_SERVICE=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --wizard)
            WIZARD_FLAG=1
            shift
            ;;
        --no-wizard)
            NO_WIZARD_FLAG=1
            shift
            ;;
        --with-tui)
            WITH_TUI=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# TEST-ONLY: SEINN_INSTALL_TEST_OS overrides uname -s so --dry-run can print
# the other OS's transcript for diffing. It must never drive a real install.
if [ -n "${SEINN_INSTALL_TEST_OS:-}" ]; then
    echo "TEST-ONLY: SEINN_INSTALL_TEST_OS=${SEINN_INSTALL_TEST_OS} overrides" \
         "uname -s — never set this in production" >&2
    if [ "$DRY_RUN" -ne 1 ]; then
        echo "install.sh: SEINN_INSTALL_TEST_OS requires --dry-run" >&2
        exit 1
    fi
fi

# run() — every mutating command routes through here so --dry-run can echo
# instead of execute.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# probe_as_user user op path — a real per-user probe, immune to os.access-
# style ACL lies (playbook: root-owned top dirs fail with a bare Permission
# denied even when os.access says yes). Unprivileged plain `test` is the
# --dry-run path the Mac test drives this under; sudo -u is what a real
# root install uses.
probe_as_user() {
    local user="$1" op="$2" path="$3"
    if [ "$EUID" -eq 0 ]; then
        sudo -u "$user" test "$op" "$path"
    else
        test "$op" "$path"
    fi
}

# wizard_share_fix_cmd op path — the exact command line offered to fix a
# share permission problem, mirroring seinn_agent.py --doctor's wording
# (_doctor_fix_unreadable / _doctor_fix_unwritable) so the two tools never
# disagree. op is "r" for read or "w" for read+write.
wizard_share_fix_cmd() {
    local op="$1" path="$2" user="$3" acl="rX"
    if [ "$op" = "w" ]; then
        acl="rwX"
    fi
    if command -v setfacl >/dev/null 2>&1; then
        printf 'setfacl -R -m u:%s:%s %s' "$user" "$acl" "$path"
    else
        printf 'chgrp -R <group> %s && chmod -R g+%s %s' "$path" "$acl" "$path"
    fi
}

# wizard_apply_share_fix op path user — routes the chosen fix through run()
# so --dry-run echoes it instead of mutating anything.
wizard_apply_share_fix() {
    local op="$1" path="$2" user="$3" acl="rX"
    if [ "$op" = "w" ]; then
        acl="rwX"
    fi
    if command -v setfacl >/dev/null 2>&1; then
        run setfacl -R -m "u:${user}:${acl}" "$path"
    else
        run chgrp -R "<group>" "$path"
        run chmod -R "g+${acl}" "$path"
    fi
}

# wizard_add_share name path — validates one share against $INSTALL_USER
# with real per-user probes, offers (never silently applies) the exact fix
# on failure, and re-prompts for a corrected path rather than ever
# proceeding with an unreadable share. Appends to SHARE_NAMES/SHARE_PATHS/
# SHARE_RW on success.
wizard_add_share() {
    local name="$1" path="$2" ans fix
    while :; do
        if ! probe_as_user "$INSTALL_USER" -d "$path"; then
            echo "not a directory: $path — missing mount? typo?"
            read -r -p "Path for '$name': " path
            continue
        fi
        if probe_as_user "$INSTALL_USER" -r "$path" && probe_as_user "$INSTALL_USER" -x "$path"; then
            break
        fi
        echo "not readable by $INSTALL_USER: $path"
        fix="$(wizard_share_fix_cmd r "$path" "$INSTALL_USER")"
        echo "fix: sudo $fix"
        echo "(or pick a different service user)"
        read -r -p "Apply this fix now? [y/N]: " ans
        if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
            wizard_apply_share_fix r "$path" "$INSTALL_USER"
        fi
        if probe_as_user "$INSTALL_USER" -r "$path" && probe_as_user "$INSTALL_USER" -x "$path"; then
            echo "now readable."
            break
        fi
        echo "still not readable — re-enter a path this user can read."
        read -r -p "Path for '$name': " path
    done

    local rw="ro"
    if probe_as_user "$INSTALL_USER" -w "$path"; then
        rw="rw"
    else
        echo "not writable by $INSTALL_USER: $path — delete-from-the-couch needs write on this folder; playback doesn't."
        fix="$(wizard_share_fix_cmd w "$path" "$INSTALL_USER")"
        echo "fix: sudo $fix"
        read -r -p "Apply this fix now? [y/N] (default continues read-only): " ans
        if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
            wizard_apply_share_fix w "$path" "$INSTALL_USER"
            if probe_as_user "$INSTALL_USER" -w "$path"; then
                rw="rw"
            else
                echo "still not writable under --dry-run (the fix didn't really run) — continuing read-only."
            fi
        fi
    fi

    SHARE_NAMES+=("$name")
    SHARE_PATHS+=("$path")
    SHARE_RW+=("$rw")
}

# wizard — the interactive setup path. Sets exactly the variables the
# existing non-interactive flow already consumes (INSTALL_USER, PORT,
# ROOTS, PREFIX, DELETE_ENABLED) and falls through into it: one install
# code path, not two. No mutation happens before the final confirm except
# fixes the user already explicitly approved at their own prompt above.
wizard() {
    echo "seinn setup wizard — press Enter to accept each [default]."
    echo

    # ---- service user ----------------------------------------------------
    local user_default="$INSTALL_USER" ans
    if [ -z "$user_default" ]; then
        if [ -n "${SUDO_USER:-}" ]; then
            user_default="$SUDO_USER"
        else
            user_default="$(id -un)"
        fi
    fi
    while :; do
        read -r -p "Service user [$user_default]: " ans
        ans="${ans:-$user_default}"
        if id -u "$ans" >/dev/null 2>&1; then
            INSTALL_USER="$ans"
            break
        fi
        if is_darwin; then
            echo "no such user: $ans — pick an existing user, or create one in" \
                 "System Settings / sysadminctl -addAccount"
        else
            echo "no such user: $ans — create one first: sudo useradd -r -s /usr/sbin/nologin seinn"
        fi
    done

    # ---- shares ------------------------------------------------------------
    SHARE_NAMES=()
    SHARE_PATHS=()
    SHARE_RW=()
    # --root flags given alongside --wizard seed the loop rather than being
    # silently dropped — each still gets a real per-user probe, not just an
    # absolute-path format check.
    local seed
    for seed in ${ROOTS[@]+"${ROOTS[@]}"}; do
        wizard_add_share "${seed%%=*}" "${seed#*=}"
    done
    ROOTS=()

    while :; do
        local sname spath more
        while :; do
            read -r -p "Share name [movies]: " sname
            sname="${sname:-movies}"
            case "$sname" in
                *[!A-Za-z0-9_-]*|"") echo "name must match [A-Za-z0-9_-]+" ;;
                *) break ;;
            esac
        done
        while :; do
            read -r -p "Path for '$sname': " spath
            case "$spath" in
                /*) break ;;
                *) echo "must be an absolute path" ;;
            esac
        done
        wizard_add_share "$sname" "$spath"
        read -r -p "Add another share? [y/N]: " more
        case "$more" in
            y|Y) ;;
            *) break ;;
        esac
    done

    # ---- port ----------------------------------------------------------
    local port_default="$PORT"
    while :; do
        read -r -p "Port [$port_default]: " ans
        ans="${ans:-$port_default}"
        case "$ans" in
            ''|*[!0-9]*)
                echo "must be digits"
                continue
                ;;
        esac
        if [ "$ans" -lt 1 ] || [ "$ans" -gt 65535 ]; then
            echo "must be between 1 and 65535"
            continue
        fi
        if python3 -c 'import socket,sys; s=socket.socket(); s.bind(("0.0.0.0",int(sys.argv[1])))' "$ans" 2>/dev/null; then
            PORT="$ans"
            break
        fi
        echo "port $ans is already in use"
        if is_darwin; then
            if command -v lsof >/dev/null 2>&1; then
                lsof -nP -iTCP:"${ans}" -sTCP:LISTEN || true
            else
                echo "holder unknown (lsof not found)"
            fi
        elif command -v ss >/dev/null 2>&1; then
            ss -ltnp "sport = :${ans}" || true
        else
            echo "holder unknown (ss not found)"
        fi
    done

    # ---- delete ----------------------------------------------------------
    echo
    echo "Delete: lets the app delete files; requires write permission on the share and the auth token."
    read -r -p "Enable delete? [off]: " ans
    case "$ans" in
        y|Y|on|yes) DELETE_ENABLED="true" ;;
        *) DELETE_ENABLED="false" ;;
    esac
    if [ "$DELETE_ENABLED" = "true" ]; then
        local n i fix
        n=${#SHARE_NAMES[@]}
        i=0
        while [ "$i" -lt "$n" ]; do
            if [ "${SHARE_RW[$i]}" = "ro" ]; then
                echo "warning: '${SHARE_NAMES[$i]}' (${SHARE_PATHS[$i]}) is read-only — delete will 500 there until it's writable."
                fix="$(wizard_share_fix_cmd w "${SHARE_PATHS[$i]}" "$INSTALL_USER")"
                echo "fix: sudo $fix"
                read -r -p "Apply this fix now? [y/N]: " ans
                if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
                    wizard_apply_share_fix w "${SHARE_PATHS[$i]}" "$INSTALL_USER"
                    if probe_as_user "$INSTALL_USER" -w "${SHARE_PATHS[$i]}"; then
                        SHARE_RW[i]="rw"
                    fi
                fi
            fi
            i=$((i + 1))
        done
    fi

    # ---- VAAPI group offer (only when /dev/dri exists) --------------------
    VAAPI_APPLIED=0
    if [ -e /dev/dri ]; then
        echo
        echo "VAAPI: needed only for seinn-convert hardware encoding, not for playback."
        read -r -p "Add $INSTALL_USER to render,video groups? [n]: " ans
        if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
            run usermod -aG render,video "$INSTALL_USER"
            VAAPI_APPLIED=1
        fi
    fi

    # ---- prefix ----------------------------------------------------------
    local prefix_default="$PREFIX"
    while :; do
        read -r -p "Install prefix [$prefix_default]: " ans
        ans="${ans:-$prefix_default}"
        case "$ans" in
            /*)
                PREFIX="$ans"
                break
                ;;
            *) echo "must be an absolute path" ;;
        esac
    done
    if [ "$PREFIX" != "$DEFAULT_PREFIX" ]; then
        PREFIX_GIVEN=1
    fi

    # ---- summary + confirm -------------------------------------------------
    echo
    echo "--- summary ---"
    echo "service user: $INSTALL_USER"
    local n i
    n=${#SHARE_NAMES[@]}
    i=0
    while [ "$i" -lt "$n" ]; do
        echo "  share: ${SHARE_NAMES[$i]} = ${SHARE_PATHS[$i]} (${SHARE_RW[$i]})"
        i=$((i + 1))
    done
    echo "port: $PORT"
    echo "delete: $DELETE_ENABLED"
    echo "prefix: $PREFIX"
    if [ "$VAAPI_APPLIED" -eq 1 ]; then
        echo "VAAPI group fix: applied"
    fi
    if [ -e "$PREFIX/seinn-agent.toml" ]; then
        echo "note: config exists at $PREFIX/seinn-agent.toml — your shares/port/delete answers will NOT be applied to it"
    fi
    echo
    read -r -p "Proceed? [y/N]: " ans
    if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
        echo "nothing was changed."
        exit 0
    fi

    i=0
    while [ "$i" -lt "$n" ]; do
        ROOTS+=("${SHARE_NAMES[$i]}=${SHARE_PATHS[$i]}")
        i=$((i + 1))
    done
}

# ---- 1. preflight -------------------------------------------------------
PY_VERSION="$(python3 --version 2>&1 | awk '{print $2}')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_REST="${PY_VERSION#*.}"
PY_MINOR="${PY_REST%%.*}"
if [ -z "$PY_MAJOR" ] || [ -z "$PY_MINOR" ]; then
    echo "seinn needs Python 3.11+ for tomllib; found $PY_VERSION" >&2
    exit 1
fi
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "seinn needs Python 3.11+ for tomllib; found $PY_VERSION" >&2
    exit 1
fi
# Darwin's plist points at this resolved interpreter — the literal
# /usr/bin/python3 the Linux unit uses is the Command Line Tools 3.9.x shim
# on macOS, below the tomllib floor.
PYTHON3="$(command -v python3)"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "WARNING: ffmpeg not found — thumbnails and durations will be off until you install ffmpeg"
fi

for spec in "${ROOTS[@]+"${ROOTS[@]}"}"; do
    path="${spec#*=}"
    case "$path" in
        /*) ;;
        *)
            echo "install.sh: --root ${spec} is not an absolute path" >&2
            exit 1
            ;;
    esac
done

# ---- 1b. wizard trigger -------------------------------------------------
# --no-wizard always wins; --wizard always forces it (even piped, even with
# other flags present — that's the scripted-test hook); otherwise it's
# no-args-on-a-TTY. Any flag present without --wizard is non-interactive,
# byte-for-byte today's behavior.
RUN_WIZARD=0
if [ "$NO_WIZARD_FLAG" -eq 1 ]; then
    RUN_WIZARD=0
elif [ "$WIZARD_FLAG" -eq 1 ]; then
    RUN_WIZARD=1
elif [ "$ARGC" -eq 0 ] && [ -t 0 ]; then
    RUN_WIZARD=1
fi

if [ "$RUN_WIZARD" -eq 1 ]; then
    wizard
fi

# ---- 2. user --------------------------------------------------------
# the service runs as this user: it must READ every root, and WRITE roots
# where delete should work — systemd runs it with no extra privileges
if [ -z "$INSTALL_USER" ]; then
    if [ -n "${SUDO_USER:-}" ]; then
        INSTALL_USER="$SUDO_USER"
    else
        INSTALL_USER="$(id -un)"
    fi
fi

# ---- 8. root privilege check -----------------------------------------
# steps 3-5 need root unless --no-service and the prefix is writable by the
# invoking user — check and fail early with which flag combination would work
# unprivileged.
if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    prefix_parent="$PREFIX"
    while [ ! -e "$prefix_parent" ] && [ "$prefix_parent" != "/" ]; do
        prefix_parent="$(dirname "$prefix_parent")"
    done
    if [ "$NO_SERVICE" -eq 1 ] && [ -w "$prefix_parent" ]; then
        : # unprivileged install into a writable prefix with no service — fine
    else
        echo "install.sh: needs root (sudo ./install.sh ...), unless run with" \
             "--no-service and a --prefix your user can write to" >&2
        exit 1
    fi
fi

# Safety interlock, checked before anything is created: a non-default --prefix
# with the default service name would collide with an existing install. A
# refusal must leave the filesystem exactly as it found it.
if [ "$NO_SERVICE" -eq 0 ] && [ "$PREFIX_GIVEN" -eq 1 ] && [ "$PREFIX" != "$DEFAULT_PREFIX" ] \
    && [ "$SERVICE_NAME_GIVEN" -eq 0 ] && [ "$SERVICE_NAME" = "$DEFAULT_SERVICE_NAME" ]; then
    echo "install.sh: custom --prefix with the default service name would" \
         "collide with an existing install — pass --service-name or --no-service" >&2
    exit 1
fi

# ---- 3. prefix ----------------------------------------------------------
run mkdir -p "$PREFIX"
run chown "$INSTALL_USER" "$PREFIX"

# the .py files are upgrade artifacts and are always safe to replace.
# seinn_convert.py and seinn_tui.py copy unconditionally (not gated on
# --with-tui): seinn_convert.py should have been shipping already, and
# seinn_tui.py is inert without the venv --with-tui creates below.
run cp "$SCRIPT_DIR/seinn_agent.py" "$PREFIX/seinn_agent.py"
run chown "$INSTALL_USER" "$PREFIX/seinn_agent.py"
run cp "$SCRIPT_DIR/seinn_convert.py" "$PREFIX/seinn_convert.py"
run chown "$INSTALL_USER" "$PREFIX/seinn_convert.py"
run cp "$SCRIPT_DIR/seinn_tui.py" "$PREFIX/seinn_tui.py"
run chown "$INSTALL_USER" "$PREFIX/seinn_tui.py"
run cp "$SCRIPT_DIR/seinn_web.html" "$PREFIX/seinn_web.html"
run chown "$INSTALL_USER" "$PREFIX/seinn_web.html"

# ---- 4. config — create only if absent, NEVER overwrite -----------------
CONFIG_PATH="$PREFIX/seinn-agent.toml"
CACHE_DIR="$PREFIX/thumbs"
STATE_DB="$PREFIX/progress.db"

roots_block=""
if [ "${#ROOTS[@]}" -gt 0 ]; then
    for spec in "${ROOTS[@]}"; do
        name="${spec%%=*}"
        path="${spec#*=}"
        roots_block="${roots_block}${name} = \"${path}\"
"
    done
else
    roots_block='# movies = "/srv/media/movies"
'
fi

write_config() {
    local token="$1"
    cat <<EOF
port = ${PORT}
bind = "0.0.0.0"
delete_enabled = ${DELETE_ENABLED}
hide_dotfiles = true
thumbs_enabled = true
cache_dir = "${CACHE_DIR}"

# Required on state-changing routes (DELETE, progress-save). Reads stay open
# on the LAN. Give it to the app once when adding this server.
auth_token = "${token}"

# Watched-state database (sqlite, created on first write).
state_db = "${STATE_DB}"

# EVERY top-level key must sit ABOVE [roots]. Appended below, TOML reads it as
# a share — which is how the auth token briefly became a publicly-listed root.
[roots]
EOF
    printf '%s' "$roots_block"
}

# NEVER overwrite an existing config. The old deploy recipe scp'd the
# repo TOML over production and wiped the live auth_token (2026-07-31).
# There is deliberately no --force path through this guard.
if [ -e "$CONFIG_PATH" ]; then
    echo "config exists — leaving it untouched (it may hold your live auth token)"
elif [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would write $CONFIG_PATH (mode 0600, owner $INSTALL_USER):"
    write_config "<generated-at-install>"
    echo
else
    token="$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")"
    umask 077
    write_config "$token" > "$CONFIG_PATH"
    chmod 0600 "$CONFIG_PATH"
    chown "$INSTALL_USER" "$CONFIG_PATH"
    echo "wrote $CONFIG_PATH"
fi

# ---- 5. service (systemd unit / launchd daemon) -------------------------
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
LABEL="com.seinn.${SERVICE_NAME#seinn-}"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
HAVE_ROOTS=0
if [ "${#ROOTS[@]}" -gt 0 ]; then
    HAVE_ROOTS=1
elif [ -e "$CONFIG_PATH" ]; then
    # config pre-existed (or was just written) without --root flags this
    # run; check whether it already has at least one configured root.
    if python3 - "$CONFIG_PATH" <<'PYEOF' >/dev/null 2>&1
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
sys.exit(0 if cfg.get("roots") else 1)
PYEOF
    then
        HAVE_ROOTS=1
    fi
fi

if is_darwin; then
    write_plist() {
        cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${PREFIX}/seinn_agent.py</string>
        <string>--config</string>
        <string>${PREFIX}/seinn-agent.toml</string>
    </array>
    <key>UserName</key>
    <string>${INSTALL_USER}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${PREFIX}/log/${SERVICE_NAME}.log</string>
    <key>StandardErrorPath</key>
    <string>${PREFIX}/log/${SERVICE_NAME}.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF
    }

    if [ "$NO_SERVICE" -eq 1 ]; then
        echo "skipping launchd daemon (--no-service)"
    else
        if [ -e "$PLIST_PATH" ] && [ "$FORCE" -eq 0 ]; then
            echo "plist exists — pass --force to rewrite it"
        else
            if [ "$DRY_RUN" -eq 1 ]; then
                echo "[dry-run] would write $PLIST_PATH:"
                write_plist
                echo
            else
                write_plist > "$PLIST_PATH"
                echo "wrote $PLIST_PATH"
            fi
            run chown root:wheel "$PLIST_PATH"
            run chmod 0644 "$PLIST_PATH"
        fi

        run mkdir -p "$PREFIX/log"
        run chown "$INSTALL_USER" "$PREFIX/log"

        # a rootless agent exits at startup and KeepAlive turns that into a
        # crash loop — only bootstrap once a root exists, mirroring the
        # systemd enable-without---now gate below.
        if [ "$HAVE_ROOTS" -eq 1 ]; then
            if [ "$DRY_RUN" -eq 1 ] || launchctl print "system/${LABEL}" >/dev/null 2>&1; then
                run launchctl bootout "system/${LABEL}"
            fi
            run launchctl bootstrap system "$PLIST_PATH"
        else
            echo "no roots configured yet — plist written but not bootstrapped."
            echo "Edit $CONFIG_PATH under [roots], then: sudo launchctl bootstrap system ${PLIST_PATH}"
        fi
    fi
elif [ "$NO_SERVICE" -eq 1 ]; then
    echo "skipping systemd unit (--no-service)"
else
    write_unit() {
        cat <<EOF
[Unit]
Description=seinn media agent (listings + range streaming + delete)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${INSTALL_USER}
ExecStart=/usr/bin/python3 ${PREFIX}/seinn_agent.py --config ${PREFIX}/seinn-agent.toml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    }

    if [ -e "$UNIT_PATH" ] && [ "$FORCE" -eq 0 ]; then
        echo "unit exists — pass --force to rewrite it"
    elif [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would write $UNIT_PATH:"
        write_unit
        echo
    else
        write_unit > "$UNIT_PATH"
        echo "wrote $UNIT_PATH"
    fi

    run systemctl daemon-reload

    # a rootless agent exits at startup and Restart=on-failure turns that
    # into a 3-second crash loop — only enable --now once a root exists.
    if [ "$HAVE_ROOTS" -eq 1 ]; then
        run systemctl enable --now "$SERVICE_NAME"
    else
        run systemctl enable "$SERVICE_NAME"
        echo "no roots configured yet — service enabled but not started."
        echo "Edit $CONFIG_PATH under [roots], then: sudo systemctl start $SERVICE_NAME"
    fi
fi

# ---- 6. TUI venv + launcher (--with-tui only) ---------------------------
# Optional and additive: a failure anywhere in here degrades to "no TUI"
# and never fails the agent install (agent + seinn-convert stay
# zero-dependency regardless of what happens below).
TUI_INSTALLED=0
TEXTUAL_VERSION=""
if [ "$WITH_TUI" -eq 1 ]; then
    TUI_INSTALLED=1
    if ! run python3 -m venv "$PREFIX/venv"; then
        TUI_INSTALLED=0
        echo "WARNING: python3 -m venv failed — continuing without the TUI" \
             "(agent install unaffected)." >&2
        if is_darwin; then
            echo "  fix: reinstall Python 3 (python.org or Homebrew), then rerun with --with-tui" >&2
        else
            echo "  fix: sudo apt install python3-venv, then rerun with --with-tui" >&2
        fi
    fi
    if [ "$TUI_INSTALLED" -eq 1 ] && ! run "$PREFIX/venv/bin/pip" install --upgrade pip; then
        TUI_INSTALLED=0
        echo "WARNING: pip upgrade failed (offline?) — continuing without the TUI" \
             "(agent install unaffected)." >&2
        echo "  fix: $PREFIX/venv/bin/pip install --upgrade pip && $PREFIX/venv/bin/pip install textual" >&2
    fi
    if [ "$TUI_INSTALLED" -eq 1 ] && ! run "$PREFIX/venv/bin/pip" install textual; then
        TUI_INSTALLED=0
        echo "WARNING: pip install textual failed (offline?) — continuing without the TUI" \
             "(agent install unaffected)." >&2
        echo "  fix: $PREFIX/venv/bin/pip install textual" >&2
    fi
    if [ "$TUI_INSTALLED" -eq 1 ]; then
        run chown -R "$INSTALL_USER" "$PREFIX/venv"
        if [ "$DRY_RUN" -eq 1 ]; then
            TEXTUAL_VERSION="<installed-at-install>"
        else
            TEXTUAL_VERSION="$("$PREFIX/venv/bin/pip" show textual 2>/dev/null | awk '/^Version: /{print $2}')"
        fi
    else
        run rm -rf "$PREFIX/venv"
    fi

    # launcher — only meaningful once the venv exists; skipped outright on
    # --no-service/unprivileged installs (no /usr/local/bin write there).
    write_launcher() {
        cat <<EOF
#!/bin/sh
# seinn TUI launcher — written by install.sh; PREFIX baked at install time.
# Safe to overwrite on every install run: unlike the config, this file
# holds no user state.
if [ -x "$PREFIX/venv/bin/python3" ]; then
    exec "$PREFIX/venv/bin/python3" "$PREFIX/seinn_tui.py" "\$@"
fi
echo "seinn: TUI not installed — rerun install.sh with --with-tui" >&2
echo "meanwhile: python3 $PREFIX/seinn_agent.py --doctor  checks your install" >&2
exit 1
EOF
    }

    if [ "$NO_SERVICE" -eq 1 ]; then
        echo "skipping TUI launcher (--no-service) — run it directly:"
        echo "  $PREFIX/venv/bin/python3 $PREFIX/seinn_tui.py"
    else
        LAUNCHER_PATH="/usr/local/bin/seinn"
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would write $LAUNCHER_PATH (mode 0755):"
            write_launcher
            echo
        else
            write_launcher > "$LAUNCHER_PATH"
            echo "wrote $LAUNCHER_PATH"
        fi
        run chmod 0755 "$LAUNCHER_PATH"
    fi
fi

# ---- 7. output (the handoff) -------------------------------------------
if is_darwin; then
    SERVER_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
else
    SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="<server-ip>"
fi

cat <<EOF

--- seinn-agent install complete ---

Health check:
  curl http://${SERVER_IP}:${PORT}/api/roots

Token handoff:
  Your auth token is in ${CONFIG_PATH} (auth_token). Enter it once in the
  seinn app when adding this server. Reads work without it; progress-saving
  and delete require it.

EOF

# The last mile: the agent mints a single-use claim code at startup and
# writes it beside the config — the printed URL below is one click from
# this terminal to a claimed browser dashboard. Best-effort: on a
# --no-service install the agent hasn't run yet, so no code exists.
CLAIM_CODE=""
for _ in 1 2 3 4 5 6; do
    if [ -f "$PREFIX/claim-code" ]; then
        CLAIM_CODE="$(cat "$PREFIX/claim-code" 2>/dev/null || true)"
        [ -n "$CLAIM_CODE" ] && break
    fi
    sleep 0.5
done
if [ -n "$CLAIM_CODE" ]; then
    cat <<EOF
Manage it in your browser:
  http://${SERVER_IP}:${PORT}/?code=${CLAIM_CODE}

EOF
else
    cat <<EOF
Manage it in your browser (the claim code prints in the agent's log at
startup, and lands in ${PREFIX}/claim-code):
  http://${SERVER_IP}:${PORT}/

EOF
fi

if [ "$NO_SERVICE" -eq 1 ]; then
    cat <<EOF
Run it (no service was installed):
  python3 ${PREFIX}/seinn_agent.py --config ${CONFIG_PATH}

Add a share:
  edit ${CONFIG_PATH} under [roots], then restart the process.
EOF
elif is_darwin; then
    cat <<EOF
Logs:
  tail -n 50 ${PREFIX}/log/${SERVICE_NAME}.log

Add a share:
  edit ${CONFIG_PATH} under [roots], then:
  sudo launchctl kickstart -k system/${LABEL}
EOF
else
    cat <<EOF
Logs:
  journalctl -u ${SERVICE_NAME} -n 50

Add a share:
  edit ${CONFIG_PATH} under [roots], then:
  sudo systemctl restart ${SERVICE_NAME}
EOF
fi

if [ "$WITH_TUI" -eq 1 ]; then
    echo
    if [ "$TUI_INSTALLED" -eq 1 ]; then
        echo "TUI (Textual ${TEXTUAL_VERSION} installed):"
        if [ "$NO_SERVICE" -eq 1 ]; then
            echo "  ${PREFIX}/venv/bin/python3 ${PREFIX}/seinn_tui.py"
        else
            echo "  seinn"
        fi
    else
        echo "TUI: not installed (see warning above) — rerun: sudo ./install.sh --with-tui"
    fi
fi
