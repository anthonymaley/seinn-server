#!/usr/bin/env bash
# seinn-agent container entrypoint.
#
# Encodes install.sh's landmine lesson (lines 543-559): an existing
# /config/seinn-agent.toml is NEVER overwritten, under any flag or env
# combination. There is deliberately no force path.
set -euo pipefail

CONFIG_DIR=/config
CONFIG_PATH=/config/seinn-agent.toml
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

log() {
    echo "seinn-entrypoint: $*"
}

if [ ! -e "$CONFIG_PATH" ]; then
    # ---- config generation — only if absent -------------------------
    if [ -z "${ROOTS:-}" ]; then
        log "ROOTS is not set and no config exists at $CONFIG_PATH — the"
        log "agent refuses to start with zero roots. Set ROOTS=\"name=/abs/path,...\""
        log "or supply a pre-written $CONFIG_PATH. Exiting rather than crash-looping."
        exit 1
    fi

    roots_block=""
    IFS=',' read -ra root_specs <<< "$ROOTS"
    for spec in "${root_specs[@]}"; do
        name="${spec%%=*}"
        path="${spec#*=}"
        if ! printf '%s' "$name" | grep -Eq '^[A-Za-z0-9_-]+$'; then
            log "invalid root name '$name' in ROOTS — must match [A-Za-z0-9_-]+"
            exit 1
        fi
        case "$path" in
            /*) ;;
            *)
                log "root '$name' path '$path' is not absolute — ROOTS entries must be name=/abs/path"
                exit 1
                ;;
        esac
        roots_block="${roots_block}${name} = \"${path}\"
"
    done

    token="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    session_secret="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"

    mkdir -p "$CONFIG_DIR"
    umask 077
    cat > "$CONFIG_PATH" <<EOF
port = 8378
bind = "0.0.0.0"
delete_enabled = false
hide_dotfiles = true
thumbs_enabled = true
cache_dir = "/config/thumbs"

# Required on state-changing routes (DELETE, progress-save). Reads stay
# open on the LAN. Give it to the app once when adding this server.
auth_token = "${token}"

# Watched-state database (sqlite, created on first write).
state_db = "/config/progress.db"

# Persisted so container restarts don't invalidate app sessions
# (unset, the agent would generate a fresh one per process).
session_secret = "${session_secret}"

# EVERY top-level key must sit ABOVE [roots]. Appended below, TOML reads
# it as a share — which is how the auth token briefly became a
# publicly-listed root.
[roots]
EOF
    printf '%s' "$roots_block" >> "$CONFIG_PATH"
    chmod 0600 "$CONFIG_PATH"

    log "generated $CONFIG_PATH with a fresh auth token"
    log "auth token: ${token}"
    log "enter this in the app when adding this server — reads work without it"
    log "health check: curl http://<host>:<published-port>/api/roots"
else
    # ---- config exists → never touch it ------------------------------
    log "config exists — leaving it untouched (it may hold your live auth token)"
    if [ -n "${ROOTS:-}" ]; then
        log "ROOTS is set but a config already exists — ROOTS ignored; edit /config/seinn-agent.toml under [roots] instead"
    fi
fi

# ---- PUID/PGID remap + drop — only when running as root ----------------
if [ "$(id -u)" -eq 0 ]; then
    groupmod -o -g "$PGID" seinn
    usermod -o -u "$PUID" seinn
    chown seinn:seinn /config
    chown -R seinn:seinn /config/thumbs 2>/dev/null || true
    chown seinn:seinn /config/seinn-agent.toml /config/progress.db 2>/dev/null || true
    log "running as seinn (uid=$PUID gid=$PGID)"
    exec gosu seinn:seinn python3 /app/seinn_agent.py --config "$CONFIG_PATH" "$@"
else
    log "started non-root (uid=$(id -u)) — PUID/PGID remap skipped"
    exec python3 /app/seinn_agent.py --config "$CONFIG_PATH" "$@"
fi
