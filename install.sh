#!/bin/bash
set -euo pipefail

# Codex Chronicle installer — managed source install from GitHub.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Sakib-Sobaha/codex-precisely/main/install.sh | bash
#
# Environment overrides:
#   CODEX_CHRONICLE_VERSION  — git ref to install. Default: main.
#   CODEX_CHRONICLE_HOME     — data + managed source root. Default: $HOME/.codex-chronicle.
#   CODEX_HOME               — Codex CLI config root. Default: $HOME/.codex.

REPO_SLUG="Sakib-Sobaha/codex-precisely"
CODEX_CHRONICLE_HOME="${CODEX_CHRONICLE_HOME:-$HOME/.codex-chronicle}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="$HOME/.local/bin"
SRC_DIR="$CODEX_CHRONICLE_HOME/src"
REF="${CODEX_CHRONICLE_VERSION:-main}"
REPO_URL="https://github.com/$REPO_SLUG.git"
HOOKS_FILE="$CODEX_HOME/hooks.json"

echo "Installing Codex Chronicle..."
echo ""

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
    Darwin/arm64|Linux/x86_64) ;;
    Darwin/x86_64)
        echo "ERROR: macOS Intel is not supported by this installer yet."
        exit 1
        ;;
    Linux/aarch64|Linux/arm64)
        echo "ERROR: Linux arm64 is not supported by this installer yet."
        exit 1
        ;;
    *)
        echo "ERROR: unsupported platform $OS/$ARCH"
        exit 1
        ;;
esac

MISSING=""
for bin in git python3 curl codex; do
    command -v "$bin" >/dev/null 2>&1 || MISSING="$MISSING $bin"
done
if [ -n "$MISSING" ]; then
    echo "ERROR: Missing required tools:$MISSING"
    echo ""
    if echo "$MISSING" | grep -q codex; then
        echo "  Install Codex CLI: https://github.com/openai/codex"
    fi
    exit 1
fi

echo "Repo:     $REPO_URL"
echo "Ref:      $REF"
echo "Codex:    $(command -v codex)"

mkdir -p "$BIN_DIR" "$CODEX_CHRONICLE_HOME" "$CODEX_HOME"

TMP_SRC="$CODEX_CHRONICLE_HOME/src.new"
OLD_SRC="$CODEX_CHRONICLE_HOME/src.old"
rm -rf "$TMP_SRC" "$OLD_SRC"

echo "Cloning repository..."
git clone --depth 1 --branch "$REF" "$REPO_URL" "$TMP_SRC/repo"

echo "Creating virtual environment..."
python3 -m venv "$TMP_SRC/venv"

echo "Installing package..."
"$TMP_SRC/venv/bin/pip" install --upgrade pip >/dev/null
"$TMP_SRC/venv/bin/pip" install -e "$TMP_SRC/repo/codex"

rm -f "$BIN_DIR/codex-chronicle" "$BIN_DIR/codex-chronicle-hook"
if [ -d "$SRC_DIR" ]; then
    mv "$SRC_DIR" "$OLD_SRC"
fi
mv "$TMP_SRC" "$SRC_DIR"
rm -rf "$OLD_SRC"

ln -sf "$SRC_DIR/venv/bin/codex-chronicle" "$BIN_DIR/codex-chronicle"
ln -sf "$SRC_DIR/venv/bin/codex-chronicle-hook" "$BIN_DIR/codex-chronicle-hook"

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

echo "Configuring Codex hooks..."
"$BIN_DIR/codex-chronicle" install-hooks "$HOOKS_FILE"

chmod 700 "$CODEX_CHRONICLE_HOME" 2>/dev/null || true

EFFECTIVE_MODE=$("$BIN_DIR/codex-chronicle" doctor 2>/dev/null | awk '/^mode:/ {print $2}')
[ -z "$EFFECTIVE_MODE" ] && EFFECTIVE_MODE="foreground"

echo ""
echo "Installed:"
echo "  $BIN_DIR/codex-chronicle      -> $SRC_DIR/venv/bin/codex-chronicle"
echo "  $BIN_DIR/codex-chronicle-hook -> $SRC_DIR/venv/bin/codex-chronicle-hook"
echo "  source:                        $SRC_DIR/repo"
echo "  version:                       $("$BIN_DIR/codex-chronicle" --version 2>/dev/null || echo 'unknown')"
echo "  mode:                          $EFFECTIVE_MODE"
echo ""
echo "Installation complete!"
echo ""
echo "Restart Codex so the hooks take effect."
echo ""
echo "Other useful commands:"
echo "  codex-chronicle doctor            # diagnose config, daemon status, drift"
echo "  codex-chronicle update            # reinstall from the latest root installer"
echo "  codex-chronicle install-daemon    # switch to background summarization mode"
echo "  codex-chronicle query timeline    # recent sessions across all projects"
