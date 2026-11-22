#!/bin/sh
# CrawlRX installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/LiquidLava1990/crawlrx/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --docker --with-ollama
#
# Options (or environment variables, for unattended installs):
#   --docker          CRAWLRX_INSTALL_MODE=docker   run the Docker stack instead of a local install
#   --with-ollama     CRAWLRX_WITH_OLLAMA=1         Docker mode: also start Ollama + a small model
#   --no-browser      CRAWLRX_SKIP_BROWSER=1        skip the Chromium download
#   --dir PATH        CRAWLRX_INSTALL_DIR=PATH      Docker mode: where to put the compose files
#   CRAWLRX_PIP_SPEC  what to pip-install (default: this repo's main branch)
set -eu

REPO="LiquidLava1990/crawlrx"
MODE="${CRAWLRX_INSTALL_MODE:-pip}"
WITH_OLLAMA="${CRAWLRX_WITH_OLLAMA:-0}"
SKIP_BROWSER="${CRAWLRX_SKIP_BROWSER:-0}"
INSTALL_DIR="${CRAWLRX_INSTALL_DIR:-$HOME/crawlrx}"
PIP_SPEC="${CRAWLRX_PIP_SPEC:-https://github.com/$REPO/archive/refs/heads/main.tar.gz}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/crawlrx"
VENV="$DATA_HOME/venv"
BIN_DIR="$HOME/.local/bin"

while [ $# -gt 0 ]; do
  case "$1" in
    --docker) MODE=docker ;;
    --with-ollama) WITH_OLLAMA=1 ;;
    --no-browser) SKIP_BROWSER=1 ;;
    --dir) shift; INSTALL_DIR="$1" ;;
    -h|--help) sed -n '2,14p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; fi
}

fetch() {  # fetch URL DEST
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "Need curl or wget."; fi
}

install_docker_stack() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Get it from https://docs.docker.com/get-docker/ or re-run without --docker."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing. Update Docker Desktop / install the compose plugin."
  say "Downloading CrawlRX to $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  tmp="$(mktemp -d)"
  fetch "https://github.com/$REPO/archive/refs/heads/main.tar.gz" "$tmp/crawlrx.tar.gz"
  tar -xzf "$tmp/crawlrx.tar.gz" -C "$INSTALL_DIR" --strip-components=1
  rm -rf "$tmp"
  cd "$INSTALL_DIR"
  if [ ! -f .env ]; then
    cp .env.example .env
    secret="$(random_secret)"
    sed "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=$secret/" .env > .env.tmp && mv .env.tmp .env
  fi
  if [ "$WITH_OLLAMA" = "1" ]; then
    say "Starting CrawlRX + SearXNG + Ollama (first model download can take a few minutes)"
    docker compose --profile ollama up -d --build
  else
    say "Starting CrawlRX + SearXNG"
    docker compose up -d --build
  fi
  say "CrawlRX is starting at http://127.0.0.1:7337  (dashboard: /dashboard, API docs: /docs)"
  say "Model check: docker compose exec crawlrx crawlrx doctor"
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

install_local() {
  PY="$(find_python)" || die "Python 3.10+ is required. Install it (macOS: brew install python; Debian/Ubuntu: sudo apt install python3 python3-venv) or re-run with --docker."
  "$PY" -c 'import venv, ensurepip' 2>/dev/null || die "Python venv support missing. Debian/Ubuntu: sudo apt install python3-venv"
  say "Creating virtual environment in $VENV"
  mkdir -p "$DATA_HOME"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  say "Installing CrawlRX from $PIP_SPEC"
  "$VENV/bin/python" -m pip install --quiet "$PIP_SPEC"
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV/bin/crawlrx" "$BIN_DIR/crawlrx"
  if [ "$SKIP_BROWSER" = "1" ]; then
    "$VENV/bin/crawlrx" setup --skip-browser || true
  else
    "$VENV/bin/crawlrx" setup || true
    if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" != "0" ]; then
      say "If Chromium fails to start, install its system libraries once with:"
      echo "    sudo $VENV/bin/python -m playwright install-deps chromium"
    fi
  fi
  RUN_CMD="crawlrx"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      RUN_CMD="$BIN_DIR/crawlrx"
      say "$BIN_DIR is not on your PATH. To fix that permanently, add this line to your shell profile:"
      echo "    export PATH=\"$BIN_DIR:\$PATH\""
      ;;
  esac
  say "Done. Start the API with:  $RUN_CMD serve    (then open http://127.0.0.1:7337/dashboard)"
}

case "$MODE" in
  docker) install_docker_stack ;;
  pip|local) install_local ;;
  *) die "CRAWLRX_INSTALL_MODE must be 'pip' or 'docker' (got '$MODE')" ;;
esac
