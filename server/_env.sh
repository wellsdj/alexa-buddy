# Shared by run.sh, ../firmware/flash.sh and ../install.sh: makes sure .venv exists with
# everything installed. If there's no Python 3.10+ on the Mac, it fetches one with uv
# (no admin password, nothing installed system-wide). Source it from anywhere.
BUDDY_SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$BUDDY_SERVER_DIR/.venv/bin/python"

_buddy_find_python() {
  local p
  for p in python3.12 python3.13 python3.11 python3.10 python3; do
    local path
    path="$(command -v "$p" 2>/dev/null)" || continue
    # Apple's /usr/bin/python3 is 3.9 and pops up a "developer tools" installer: skip it.
    [ "$path" = "/usr/bin/python3" ] && continue
    if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$path"
      return 0
    fi
  done
  return 1
}

_buddy_uv() {
  local uv="$HOME/.local/share/buddy/uv"
  [ -x "$uv" ] && { echo "$uv"; return 0; }
  command -v uv >/dev/null 2>&1 && { command -v uv; return 0; }
  local arch os
  arch="$(uname -m)"; [ "$arch" = "arm64" ] && arch="aarch64"
  case "$(uname)" in Darwin) os="apple-darwin" ;; *) os="unknown-linux-gnu" ;; esac
  echo "Getting Python (one-off download, about 50 MB)..." >&2
  mkdir -p "$HOME/.local/share/buddy"
  curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-$os.tar.gz" \
    | tar xz -C "$HOME/.local/share/buddy" --strip-components 1 || return 1
  [ -x "$uv" ] && echo "$uv"
}

buddy_env() {
  local dir="$BUDDY_SERVER_DIR"
  if [ ! -x "$VENV_PY" ]; then
    echo "Setting up Buddy (first time only, a few minutes)..."
    local py=""
    [ -z "$BUDDY_FORCE_UV" ] && py="$(_buddy_find_python || true)"
    if [ -n "$py" ]; then
      "$py" -m venv "$dir/.venv" && "$VENV_PY" -m pip install -q --upgrade pip
    else
      local uv
      uv="$(_buddy_uv)" || { echo "Couldn't get Python. Check the internet connection and try again."; return 1; }
      "$uv" venv -q --python 3.12 "$dir/.venv" || return 1
      touch "$dir/.venv/.uv"
    fi
    rm -f "$dir/.venv/.installed"
  fi
  local want
  want="$(cksum < "$dir/requirements.txt")"
  if [ "$(cat "$dir/.venv/.installed" 2>/dev/null)" != "$want" ]; then
    echo "Installing Buddy's parts (takes 2-5 minutes; it's working even if nothing moves)..."
    if [ -f "$dir/.venv/.uv" ]; then
      "$(_buddy_uv)" pip install -q --python "$VENV_PY" -r "$dir/requirements.txt" || return 1
    else
      "$VENV_PY" -m pip install -q -r "$dir/requirements.txt" || return 1
    fi
    echo "$want" > "$dir/.venv/.installed"
  fi
  "$VENV_PY" "$dir/setup_models.py" || return 1
}
