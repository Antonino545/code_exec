#!/usr/bin/env bash
set -e

# Default installation directory
INSTALL_DIR="${CODE_EXEC_DIR:-$HOME/.code_exec_app}"
REPO_URL="https://github.com/antonino54/code_exec.git"

BOLD='\033[1m'
CYAN='\033[38;2;137;220;235m'
GREEN='\033[38;2;166;227;161m'
AMBER='\033[38;2;249;226;175m'
RED='\033[38;2;243;139;168m'
RESET='\033[0m'

echo -e "${BOLD}${CYAN}=== code-exec installer ===${RESET}"

# 1. Verify python3 availability
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 is not installed or not in PATH.${RESET}" >&2
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MAJOR" -eq 3 -a "$PY_MINOR" -lt 8 ]; then
    echo -e "${RED}Error: Python 3.8+ is required (found $PY_VERSION).${RESET}" >&2
    exit 1
fi

# 2. Acquire or link repository source
if [ -f "./code_exec.py" ]; then
    INSTALL_DIR="$(pwd)"
    echo -e "${GREEN}✓${RESET} Detected local repository at ${BOLD}$INSTALL_DIR${RESET}"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo -e "${CYAN}●${RESET} Updating existing installation in ${BOLD}$INSTALL_DIR${RESET}..."
        git -C "$INSTALL_DIR" pull --ff-only || true
    else
        echo -e "${CYAN}●${RESET} Cloning code_exec repository to ${BOLD}$INSTALL_DIR${RESET}..."
        git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    fi
fi

# 3. Determine binary destination
BIN_DIR=""
if [ -w "/usr/local/bin" ]; then
    BIN_DIR="/usr/local/bin"
elif [ -d "$HOME/.local/bin" ] || mkdir -p "$HOME/.local/bin" 2>/dev/null; then
    BIN_DIR="$HOME/.local/bin"
else
    BIN_DIR="/usr/local/bin"
fi

TARGET="$BIN_DIR/code-exec"

# 4. Generate launcher script
LAUNCHER_SCRIPT="#!/usr/bin/env bash\nexec python3 \"$INSTALL_DIR/code_exec.py\" \"\$@\""

if [ -w "$BIN_DIR" ]; then
    printf "$LAUNCHER_SCRIPT\n" > "$TARGET"
    chmod +x "$TARGET"
else
    echo -e "${AMBER}!${RESET} Writing to $TARGET requires administrative permissions:"
    printf "$LAUNCHER_SCRIPT\n" | sudo tee "$TARGET" >/dev/null
    sudo chmod +x "$TARGET"
fi

echo -e "${GREEN}✓${RESET} Binary installed at: ${BOLD}$TARGET${RESET}"

# 5. Linux clipboard check
if [ "$(uname -s)" = "Linux" ]; then
    if ! command -v xclip >/dev/null 2>&1 && ! command -v wl-copy >/dev/null 2>&1; then
        echo -e "${AMBER}! Warning: No system clipboard utility detected.${RESET}"
        echo -e "  Install with: ${BOLD}sudo apt install -y xclip${RESET} (X11) or ${BOLD}sudo apt install -y wl-clipboard${RESET} (Wayland)"
    fi
fi

# 6. Ensure PATH inclusion
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo -e "${AMBER}! Note: $BIN_DIR is not in your current PATH.${RESET}"
        SHELL_RC=""
        if [ -n "$ZSH_VERSION" ] || [ -f "$HOME/.zshrc" ]; then
            SHELL_RC="$HOME/.zshrc"
        elif [ -f "$HOME/.bashrc" ]; then
            SHELL_RC="$HOME/.bashrc"
        fi
        if [ -n "$SHELL_RC" ]; then
            echo "export PATH=\"\$PATH:$BIN_DIR\"" >> "$SHELL_RC"
            echo -e "  Added to ${BOLD}$SHELL_RC${RESET}. Run: ${BOLD}source $SHELL_RC${RESET}"
        fi
        ;;
esac

echo -e "\n${BOLD}${GREEN}Installation complete!${RESET} Run ${CYAN}code-exec --help${RESET} to get started."
