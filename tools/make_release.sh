#!/usr/bin/env bash
# make_release.sh — build a distributable seinn-agent release tarball.
#
# Reads AGENT_VERSION from seinn_agent.py (single source of truth),
# assembles dist/seinn-agent-<version>/ with the agent, installer, example
# config, README, and a generated RELEASE file, then tars and checksums it.
#
# Usage: tools/make_release.sh
#
# Idempotent: re-running overwrites the dist/ output for this version cleanly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$REPO_ROOT"
DIST_DIR="$REPO_ROOT/dist"

AGENT_PY="$SERVER_DIR/seinn_agent.py"
if [ ! -f "$AGENT_PY" ]; then
    echo "make_release.sh: not found: $AGENT_PY" >&2
    exit 1
fi

VERSION="$(grep -E '^AGENT_VERSION = "' "$AGENT_PY" | head -n1 | sed -E 's/^AGENT_VERSION = "([^"]+)".*/\1/')"
if [ -z "$VERSION" ]; then
    echo "make_release.sh: could not find AGENT_VERSION in $AGENT_PY" >&2
    exit 1
fi

REQUIRED_FILES=(
    "seinn_agent.py"
    "seinn_convert.py"
    "seinn_tui.py"
    "install.sh"
    "seinn-agent.toml.example"
    "README.md"
    "Dockerfile"
    "docker-entrypoint.sh"
    "docker-compose.example.yml"
)
for f in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$SERVER_DIR/$f" ]; then
        echo "make_release.sh: not found: $SERVER_DIR/$f" >&2
        exit 1
    fi
done

RELEASE_NAME="seinn-agent-${VERSION}"
STAGE_DIR="$DIST_DIR/$RELEASE_NAME"
TARBALL="$DIST_DIR/${RELEASE_NAME}.tar.gz"
CHECKSUM="$TARBALL.sha256"

# ---- clean & stage ------------------------------------------------------
mkdir -p "$DIST_DIR"
rm -rf "$STAGE_DIR" "$TARBALL" "$CHECKSUM"
mkdir -p "$STAGE_DIR"

for f in "${REQUIRED_FILES[@]}"; do
    cp "$SERVER_DIR/$f" "$STAGE_DIR/$f"
done
chmod +x "$STAGE_DIR/install.sh"
chmod +x "$STAGE_DIR/docker-entrypoint.sh"

# Wrinkle: the Dockerfile's `COPY tools/krutho_selftest.py /app/tools/`
# references a path outside the four Docker files above; the staging step
# must carry it too, or a tarball-context `docker build` fails.
mkdir -p "$STAGE_DIR/tools"
cp "$SERVER_DIR/tools/krutho_selftest.py" "$STAGE_DIR/tools/"

GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
DATE="$(date -u +%Y-%m-%d)"

cat > "$STAGE_DIR/RELEASE" <<EOF
version: ${VERSION}
date: ${DATE}
commit: ${GIT_SHA}
EOF

# ---- tar & checksum -------------------------------------------------------
( cd "$DIST_DIR" && tar -czf "${RELEASE_NAME}.tar.gz" "$RELEASE_NAME" )
( cd "$DIST_DIR" && shasum -a 256 "${RELEASE_NAME}.tar.gz" > "${RELEASE_NAME}.tar.gz.sha256" )

echo
echo "--- release built ---"
echo "version:  $VERSION"
echo "staged:   $STAGE_DIR"
echo "tarball:  $TARBALL"
echo "checksum: $CHECKSUM"
echo
echo "Install (on the target server):"
echo "  tar -xzf ${RELEASE_NAME}.tar.gz && cd ${RELEASE_NAME} && sudo ./install.sh --root name=/path"
echo
echo "Docker image (local build only — no registry image is"
echo "published yet):"
echo "  cd ${RELEASE_NAME} && docker build --build-arg AGENT_VERSION=${VERSION} -t seinn-agent:${VERSION} ."
