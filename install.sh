#!/bin/bash
set -euo pipefail

# Codex Chronicle installer — downloads a prebuilt binary release.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Sakib-Sobaha/codex-precisely/main/codex/install.sh | bash
#
# Environment overrides (for testing / pinning):
#   CODEX_CHRONICLE_VERSION  — git tag, e.g. vX.Y.Z. Default: latest release.
#   CODEX_CHRONICLE_BASE_URL — override download host (e.g. local mirror).
#   CODEX_CHRONICLE_HOME     — data + runtime root. Default: $HOME/.codex-chronicle.
#   CODEX_HOME               — Codex CLI config root. Default: $HOME/.codex.

REPO_SLUG="Sakib-Sobaha/codex-precisely"
CODEX_CHRONICLE_HOME="${CODEX_CHRONICLE_HOME:-$HOME/.codex-chronicle}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="$HOME/.local/bin"
RUNTIME_DIR="$CODEX_CHRONICLE_HOME/runtime"
VERSION="${CODEX_CHRONICLE_VERSION:-latest}"
BASE_URL="${CODEX_CHRONICLE_BASE_URL:-https://github.com/$REPO_SLUG/releases}"
HOOKS_FILE="$CODEX_HOME/hooks.json"

echo "Installing Codex Chronicle..."
echo ""

# -----------------------------------------------------------------------------
# 1. Detect platform
# -----------------------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
    Darwin/arm64)       TARGET="darwin-arm64" ;;
    Linux/x86_64)       TARGET="linux-x86_64" ;;
    Darwin/x86_64)
        echo "ERROR: macOS Intel is not a prebuilt target yet."
        echo "  Build locally: git clone git@github.com:$REPO_SLUG.git && cd codex-precisely/codex && pip install pyinstaller -e . && pyinstaller --name codex-chronicle --onedir --clean --noupx codex_chronicle/_entrypoint.py"
        exit 1
        ;;
    Linux/aarch64|Linux/arm64)
        echo "ERROR: Linux arm64 is not a prebuilt target yet."
        echo "  File an issue or build locally (same recipe as above)."
        exit 1
        ;;
    *)
        echo "ERROR: unsupported platform $OS/$ARCH"
        exit 1
        ;;
esac
echo "Platform: $TARGET"

# -----------------------------------------------------------------------------
# 2. Check dependencies
# -----------------------------------------------------------------------------
MISSING=""
for bin in curl tar; do
    command -v "$bin" >/dev/null 2>&1 || MISSING="$MISSING $bin"
done
CODEX_FOUND=""
if command -v codex >/dev/null 2>&1; then
    CODEX_FOUND="$(command -v codex)"
else
    for d in "$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
        if [ -x "$d/codex" ]; then
            CODEX_FOUND="$d/codex"
            break
        fi
    done
fi
[ -z "$CODEX_FOUND" ] && MISSING="$MISSING codex"

if [ -n "$MISSING" ]; then
    echo "ERROR: Missing required tools:$MISSING"
    echo ""
    if echo "$MISSING" | grep -q codex; then
        echo "  Install Codex CLI: https://github.com/openai/codex"
    fi
    exit 1
fi
echo "Codex:    $CODEX_FOUND"

# -----------------------------------------------------------------------------
# 3. Resolve download URLs
# -----------------------------------------------------------------------------
if [ "$VERSION" = "latest" ]; then
    ASSET_URL="$BASE_URL/latest/download/codex-chronicle-$TARGET.tar.gz"
    SHA_URL="$BASE_URL/latest/download/codex-chronicle-$TARGET.tar.gz.sha256"
else
    ASSET_URL="$BASE_URL/download/$VERSION/codex-chronicle-$TARGET.tar.gz"
    SHA_URL="$BASE_URL/download/$VERSION/codex-chronicle-$TARGET.tar.gz.sha256"
fi
echo "Asset:    $ASSET_URL"

# -----------------------------------------------------------------------------
# 4. Download + verify + extract
# -----------------------------------------------------------------------------
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
cd "$TMPDIR"

echo "Downloading..."
curl -fL --progress-bar -o codex-chronicle.tar.gz "$ASSET_URL"
curl -fsSL -o codex-chronicle.tar.gz.sha256 "$SHA_URL"

echo "Verifying SHA256..."
EXPECTED=$(awk '{print $1}' codex-chronicle.tar.gz.sha256)
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum codex-chronicle.tar.gz | awk '{print $1}')
else
    ACTUAL=$(shasum -a 256 codex-chronicle.tar.gz | awk '{print $1}')
fi
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "ERROR: SHA256 mismatch"
    echo "  expected: $EXPECTED"
    echo "  actual:   $ACTUAL"
    exit 1
fi
echo "SHA256 ok: $ACTUAL"

echo "Extracting..."
tar -xzf codex-chronicle.tar.gz

# -----------------------------------------------------------------------------
# 5. Stop daemon if running
# -----------------------------------------------------------------------------
DAEMON_WAS_RUNNING=0
if [ -f "$CODEX_CHRONICLE_HOME/daemon.pid" ]; then
    DAEMON_PID=$(cat "$CODEX_CHRONICLE_HOME/daemon.pid" 2>/dev/null || echo "")
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        DAEMON_WAS_RUNNING=1
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        sleep 1
    fi
fi

rm -f "$BIN_DIR/codex-chronicle" "$BIN_DIR/codex-chronicle-hook"

# -----------------------------------------------------------------------------
# 6. Install runtime + symlinks
# -----------------------------------------------------------------------------
mkdir -p "$BIN_DIR" "$CODEX_CHRONICLE_HOME"

NEW_RUNTIME="$CODEX_CHRONICLE_HOME/runtime.new"
rm -rf "$NEW_RUNTIME"
mv "codex-chronicle-$TARGET" "$NEW_RUNTIME"

if [ -d "$RUNTIME_DIR" ]; then
    OLD_RUNTIME="$CODEX_CHRONICLE_HOME/runtime.old"
    rm -rf "$OLD_RUNTIME"
    mv "$RUNTIME_DIR" "$OLD_RUNTIME"
fi
mv "$NEW_RUNTIME" "$RUNTIME_DIR"
rm -rf "$CODEX_CHRONICLE_HOME/runtime.old"

if [ "$OS" = "Darwin" ]; then
    xattr -dr com.apple.quarantine "$RUNTIME_DIR" 2>/dev/null || true
fi

ln -sf "$RUNTIME_DIR/codex-chronicle" "$BIN_DIR/codex-chronicle"
ln -sf "codex-chronicle" "$BIN_DIR/codex-chronicle-hook"

# -----------------------------------------------------------------------------
# 7. PATH check
# -----------------------------------------------------------------------------
if ! echo ":$PATH:" | grep -qF ":$BIN_DIR:"; then
    SHELL_RC=""
    case "$(basename "${SHELL:-}")" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac
    EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if ! grep -qF "$EXPORT_LINE" "$SHELL_RC" 2>/dev/null; then
        echo "$EXPORT_LINE" >> "$SHELL_RC"
        echo "Added ~/.local/bin to PATH in $SHELL_RC"
    fi
    export PATH="$BIN_DIR:$PATH"
fi

# -----------------------------------------------------------------------------
# 8. Configure hooks + feature flag (via the binary we just installed)
# -----------------------------------------------------------------------------
echo "Configuring Codex hooks..."
mkdir -p "$CODEX_HOME"
"$BIN_DIR/codex-chronicle" install-hooks "$HOOKS_FILE"

# -----------------------------------------------------------------------------
# 9. Tighten data dir perms + restart daemon if needed
# -----------------------------------------------------------------------------
chmod 700 "$CODEX_CHRONICLE_HOME" 2>/dev/null || true

EFFECTIVE_MODE=$("$BIN_DIR/codex-chronicle" doctor 2>/dev/null | awk '/^mode:/ {print $2}')
[ -z "$EFFECTIVE_MODE" ] && EFFECTIVE_MODE="foreground"

if [ "$EFFECTIVE_MODE" = "background" ]; then
    if [ "$OS" = "Darwin" ]; then
        if launchctl print "gui/$(id -u)/com.codex-chronicle.daemon" >/dev/null 2>&1; then
            launchctl kickstart -k "gui/$(id -u)/com.codex-chronicle.daemon" >/dev/null 2>&1 \
                && echo "Kickstarted launchd daemon (new binary active)." \
                || echo "  (launchctl kickstart failed; daemon will pick up new binary on next restart)"
        fi
    elif [ "$OS" = "Linux" ]; then
        if systemctl --user is-active --quiet codex-chronicle-daemon.service 2>/dev/null; then
            systemctl --user restart codex-chronicle-daemon.service \
                && echo "Restarted systemd daemon (new binary active)." \
                || echo "  (systemctl restart failed; daemon will pick up new binary on next restart)"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 10. Verify + summary
# -----------------------------------------------------------------------------
echo ""
echo "Installed:"
echo "  $BIN_DIR/codex-chronicle      -> $RUNTIME_DIR/codex-chronicle"
echo "  $BIN_DIR/codex-chronicle-hook -> codex-chronicle"
echo "  runtime:                       $RUNTIME_DIR  ($(du -sh "$RUNTIME_DIR" 2>/dev/null | awk '{print $1}'))"
echo "  version:                       $("$BIN_DIR/codex-chronicle" --version 2>/dev/null || echo 'unknown')"
echo "  mode:                          $EFFECTIVE_MODE"
echo ""
echo "Installation complete!"
echo ""
echo "Restart Codex so the hooks take effect."
echo ""
echo "Other useful commands:"
echo "  codex-chronicle doctor            # diagnose config, daemon status, drift"
echo "  codex-chronicle update            # fetch and install the latest release"
echo "  codex-chronicle install-daemon    # switch to background summarization mode"
echo "  codex-chronicle query timeline    # recent sessions across all projects"
