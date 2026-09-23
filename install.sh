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

if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MAJOR" -eq 3 -a "$PY_MINOR" -lt 9 ]; then
    echo -e "${RED}Error: Python 3.9+ is required (found $PY_VERSION).${RESET}" >&2
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

# 3. Determine binary destination (prefer writable user bin for clean piped curl install)
mkdir -p "$HOME/.local/bin" 2>/dev/null || true

if [ -w "/usr/local/bin" ]; then
    BIN_DIR="/usr/local/bin"
elif [ -d "$HOME/.local/bin" ] && [ -w "$HOME/.local/bin" ]; then
    BIN_DIR="$HOME/.local/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

TARGET="$BIN_DIR/code-exec"

# 4. Generate launcher script
LAUNCHER_SCRIPT="#!/usr/bin/env bash
exec python3 \"$INSTALL_DIR/code_exec.py\" \"\$@\""

if [ -w "$BIN_DIR" ]; then
    printf "%s\n" "$LAUNCHER_SCRIPT" > "$TARGET"
    chmod +x "$TARGET"
else
    echo -e "${AMBER}!${RESET} Writing to $TARGET requires administrative permissions:"
    # Use /dev/tty if stdin is tied to curl pipe
    if [ -t 0 ]; then
        printf "%s\n" "$LAUNCHER_SCRIPT" | sudo tee "$TARGET" >/dev/null
        sudo chmod +x "$TARGET"
    else
        printf "%s\n" "$LAUNCHER_SCRIPT" | sudo tee "$TARGET" >/dev/null </dev/tty
        sudo chmod +x "$TARGET" </dev/tty
    fi
fi

echo -e "${GREEN}✓${RESET} Binary installed at: ${BOLD}$TARGET${RESET}"

# 5. Linux clipboard check
if [ "$(uname -s)" = "Linux" ]; then
    if ! command -v xclip >/dev/null 2>&1 && ! command -v wl-copy >/dev/null 2>&1; then
        echo -e "${AMBER}! Warning: No system clipboard utility detected.${RESET}"
        echo -e "  Install with: ${BOLD}sudo apt install -y xclip${RESET} (X11) or ${BOLD}sudo apt install -y wl-clipboard${RESET} (Wayland)"
    fi
fi

# 6. Ensure persistent PATH inclusion
add_to_path() {
    local rc_file="$1"
    local bin_path="$2"
    if [ -f "$rc_file" ]; then
        if ! grep -qs "$bin_path" "$rc_file"; then
            printf "\n# code-exec path\nexport PATH=\"%s:\$PATH\"\n" "$bin_path" >> "$rc_file"
            echo -e "${GREEN}✓${RESET} Added ${BOLD}$bin_path${RESET} to ${BOLD}$rc_file${RESET}"
            UPDATED_RC="$rc_file"
        fi
    fi
}

UPDATED_RC=""
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        # Detect shell configuration files
        add_to_path "$HOME/.zshrc" "$BIN_DIR"
        add_to_path "$HOME/.bashrc" "$BIN_DIR"
        add_to_path "$HOME/.bash_profile" "$BIN_DIR"
        add_to_path "$HOME/.profile" "$BIN_DIR"

        if [ -n "$UPDATED_RC" ]; then
            echo -e "${AMBER}!${RESET} To update your current terminal session, run: ${BOLD}source $UPDATED_RC${RESET}"
        else
            echo -e "${AMBER}!${RESET} Please ensure ${BOLD}$BIN_DIR${RESET} is included in your PATH."
        fi
        ;;
esac

echo -e "\n${BOLD}${GREEN}Installation complete!${RESET} Run ${CYAN}code-exec --help${RESET} to get started."
