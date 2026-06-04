#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/leo-yli/voice-hermes-plugin.git}"
BRANCH="${BRANCH:-main}"
PLUGIN_NAME="${PLUGIN_NAME:-xalgo-voice-platform}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="${PLUGIN_DIR:-$HERMES_HOME/plugins/$PLUGIN_NAME}"
CONFIG_FILE="${CONFIG_FILE:-$HERMES_HOME/config.yaml}"
SKIP_DEPS="${SKIP_DEPS:-0}"

log() {
  printf '[voice-hermes-plugin] %s\n' "$*"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

find_python() {
  if [ -n "${HERMES_PYTHON:-}" ]; then
    printf '%s\n' "$HERMES_PYTHON"
    return
  fi
  if [ -x "$HERMES_HOME/hermes-agent/venv/bin/python" ]; then
    printf '%s\n' "$HERMES_HOME/hermes-agent/venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  printf 'Missing required command: python3\n' >&2
  exit 1
}

find_uv() {
  if [ -n "${HERMES_UV:-}" ]; then
    printf '%s\n' "$HERMES_UV"
    return
  fi
  if [ -x "$HERMES_HOME/bin/uv" ]; then
    printf '%s\n' "$HERMES_HOME/bin/uv"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  return 1
}

install_or_update_plugin() {
  mkdir -p "$HERMES_HOME/plugins"

  if [ -d "$PLUGIN_DIR/.git" ]; then
    log "Updating existing plugin at $PLUGIN_DIR"
    git -C "$PLUGIN_DIR" remote set-url origin "$REPO_URL"
    git -C "$PLUGIN_DIR" fetch origin "$BRANCH"
    if git -C "$PLUGIN_DIR" rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
      git -C "$PLUGIN_DIR" checkout "$BRANCH"
    else
      git -C "$PLUGIN_DIR" checkout -b "$BRANCH" "origin/$BRANCH"
    fi
    git -C "$PLUGIN_DIR" pull --ff-only origin "$BRANCH"
    return
  fi

  if [ -e "$PLUGIN_DIR" ]; then
    backup="${PLUGIN_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    log "Existing non-git plugin directory found; moving it to $backup"
    mv "$PLUGIN_DIR" "$backup"
  fi

  log "Cloning $REPO_URL into $PLUGIN_DIR"
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$PLUGIN_DIR"
}

install_dependencies() {
  if [ "$SKIP_DEPS" = "1" ]; then
    log "Skipping Python dependency installation because SKIP_DEPS=1"
    return
  fi

  py="$(find_python)"
  requirements="$PLUGIN_DIR/requirements.txt"
  if [ ! -f "$requirements" ]; then
    log "No requirements.txt found; skipping dependencies"
    return
  fi

  if "$py" -m pip --version >/dev/null 2>&1; then
    log "Installing Python dependencies with $py"
    if "$py" -m pip install -r "$requirements"; then
      return
    fi

    log "Plain pip install failed; retrying with --user"
    if "$py" -m pip install --user -r "$requirements"; then
      return
    fi
  else
    log "pip is not available for $py"
  fi

  if uv="$(find_uv)"; then
    log "Installing Python dependencies with uv using $py"
    "$uv" pip install --python "$py" -r "$requirements"
    return
  fi

  printf 'Python dependency installation failed and uv was not found.\n' >&2
  exit 1
}

enable_plugin() {
  mkdir -p "$(dirname "$CONFIG_FILE")"

  if [ ! -f "$CONFIG_FILE" ]; then
    log "Creating $CONFIG_FILE"
    cat >"$CONFIG_FILE" <<EOF
plugins:
  enabled:
    - $PLUGIN_NAME
EOF
    return
  fi

  if grep -qE "^[[:space:]]*-[[:space:]]*$PLUGIN_NAME([[:space:]]*#.*)?$" "$CONFIG_FILE"; then
    log "Plugin already enabled in $CONFIG_FILE"
    return
  fi

  backup="${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
  cp "$CONFIG_FILE" "$backup"
  log "Backed up config to $backup"

  CONFIG_FILE="$CONFIG_FILE" PLUGIN_NAME="$PLUGIN_NAME" "$(find_python)" <<'PY'
from __future__ import annotations

import os
from pathlib import Path

path = Path(os.environ["CONFIG_FILE"])
plugin = os.environ["PLUGIN_NAME"]
text = path.read_text(encoding="utf-8")
lines = text.splitlines()

if any(line.strip() == f"- {plugin}" for line in lines):
    raise SystemExit(0)

def leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))

plugins_idx = next((i for i, line in enumerate(lines) if line.strip() == "plugins:" and leading_spaces(line) == 0), None)

if plugins_idx is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(["plugins:", "  enabled:", f"    - {plugin}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raise SystemExit(0)

next_top = len(lines)
for i in range(plugins_idx + 1, len(lines)):
    if lines[i].strip() and leading_spaces(lines[i]) == 0:
        next_top = i
        break

enabled_idx = None
for i in range(plugins_idx + 1, next_top):
    if lines[i].strip() == "enabled:" and leading_spaces(lines[i]) == 2:
        enabled_idx = i
        break

if enabled_idx is None:
    lines[plugins_idx + 1:plugins_idx + 1] = ["  enabled:", f"    - {plugin}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raise SystemExit(0)

insert_at = enabled_idx + 1
while insert_at < next_top:
    stripped = lines[insert_at].strip()
    indent = leading_spaces(lines[insert_at])
    if stripped and indent <= 2:
        break
    insert_at += 1

lines.insert(insert_at, f"    - {plugin}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  log "Enabled $PLUGIN_NAME in $CONFIG_FILE"
}

main() {
  need_cmd git
  install_or_update_plugin
  install_dependencies
  enable_plugin

  log "Installation complete."
  log "Next: run 'hermes gateway setup', choose 'Xalgo Voice', bind with the 8-character code, then restart the gateway."
}

main "$@"
