#!/usr/bin/env bash
# Installs `shit`: symlinks shit-cli onto PATH, wires the shell function into
# .bashrc/.zshrc, and makes sure Ollama (+ a small model) is installed and
# running - all in user-space, no root required.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
MODEL="${SHIT_MODEL:-qwen2.5:0.5b}"

echo "Installing shit-cli to $BIN_DIR ..."
mkdir -p "$BIN_DIR"
ln -sf "$REPO_DIR/shit_cli.py" "$BIN_DIR/shit-cli"
chmod +x "$REPO_DIR/shit_cli.py"

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
SOURCE_LINE="source \"$REPO_DIR/shell/integration.sh\""

wire_rcfile() {
  local rcfile="$1"
  [ -f "$rcfile" ] || return 0

  local needs_header=false
  if ! grep -qF "$PATH_LINE" "$rcfile" 2>/dev/null || ! grep -qF "$REPO_DIR/shell/integration.sh" "$rcfile" 2>/dev/null; then
    needs_header=true
  fi
  $needs_header || { echo "shit already wired into $rcfile"; return 0; }

  {
    echo ""
    echo "# shit: fix your last broken command (https://github.com/badudum/shit)"
    grep -qF "$PATH_LINE" "$rcfile" 2>/dev/null || echo "$PATH_LINE"
    grep -qF "$REPO_DIR/shell/integration.sh" "$rcfile" 2>/dev/null || echo "$SOURCE_LINE"
  } >> "$rcfile"
  echo "Wired shit into $rcfile"
}

[ -f "$HOME/.bashrc" ] && wire_rcfile "$HOME/.bashrc"
[ -f "$HOME/.zshrc" ] && wire_rcfile "$HOME/.zshrc"

# make ollama/shit-cli usable for the rest of *this* script too
export PATH="$BIN_DIR:$PATH"

# --- Ollama: install (user-space, no sudo) --------------------------------

install_ollama_userspace() {
  echo "Installing Ollama to $BIN_DIR (no root needed) ..."
  local arch tmp
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "shit: unsupported architecture $(uname -m), install Ollama manually from https://ollama.com/download" >&2; return 1 ;;
  esac
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  if command -v zstd >/dev/null 2>&1 && \
     curl --fail --silent --head --location "https://ollama.com/download/ollama-linux-${arch}.tar.zst" >/dev/null 2>&1; then
    curl --fail --show-error --location --progress-bar \
      "https://ollama.com/download/ollama-linux-${arch}.tar.zst" -o "$tmp/ollama.tar.zst" || return 1
    zstd -d "$tmp/ollama.tar.zst" -o "$tmp/ollama.tar" || return 1
    tar -xf "$tmp/ollama.tar" -C "$HOME/.local" || return 1
  else
    curl --fail --show-error --location --progress-bar \
      "https://ollama.com/download/ollama-linux-${arch}.tgz" -o "$tmp/ollama.tgz" || return 1
    tar -xzf "$tmp/ollama.tgz" -C "$HOME/.local" || return 1
  fi
  [ -x "$BIN_DIR/ollama" ]
}

if ! command -v ollama >/dev/null 2>&1; then
  echo ""
  echo "Ollama isn't installed. shit needs it to run the local model."
  install_ollama_userspace || {
    echo "shit: couldn't install Ollama automatically. Install it yourself from https://ollama.com/download, then re-run this script." >&2
    exit 1
  }
fi

# --- Ollama: make sure the server is running (persistently, no sudo) -----

ollama_is_up() { curl -fsS -o /dev/null "http://127.0.0.1:11434/" 2>/dev/null; }

if ! ollama_is_up; then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    echo "Setting up a systemd --user service so Ollama survives logins/reboots ..."
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/ollama.service" <<EOF
[Unit]
Description=Ollama local LLM server

[Service]
ExecStart=$BIN_DIR/ollama serve
Restart=on-failure
Environment=PATH=$BIN_DIR:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now ollama.service
  else
    echo "No user systemd instance found; starting ollama serve in the background instead."
    echo "(it won't restart automatically on reboot - add 'ollama serve &' to your shell rc if you want that)"
    nohup "$BIN_DIR/ollama" serve >"$HOME/.local/share/ollama-serve.log" 2>&1 &
    disown
  fi

  echo -n "Waiting for Ollama to come up "
  for _ in $(seq 1 30); do
    ollama_is_up && break
    echo -n "."
    sleep 1
  done
  echo ""
  ollama_is_up || { echo "shit: Ollama still isn't responding on :11434, check the logs above." >&2; exit 1; }
fi
echo "Ollama is running."

# --- Ollama: pull the model -------------------------------------------------

if ! ollama list 2>/dev/null | grep -q "^${MODEL%%:*}"; then
  echo "Pulling model $MODEL (small, this only happens once) ..."
  ollama pull "$MODEL" || exit 1
else
  echo "Model $MODEL already present."
fi

echo ""
echo "Done. Restart your shell (or 'source ~/.bashrc' / 'source ~/.zshrc'), then try:"
echo "    git bush"
echo "    shit"
