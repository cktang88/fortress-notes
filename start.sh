#!/usr/bin/env bash
# Start Fortress Notes: set up the backend, then build and open the Mac app, which
# starts the backend itself (with a fresh access token) and stops it when you quit.
#
#   ./start.sh            backend setup + Mac app (the normal way)
#   ./start.sh --backend  only the backend, in this terminal (for development; no token,
#                         loopback only), at http://127.0.0.1:8000
#
# Requirements: uv (https://docs.astral.sh/uv/), and for the app macOS 26 with Xcode 26.
# Rust (cargo) is optional: with it, the OneNote converter is built for imports.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode="${1:-app}"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$mode" in
  app | --app) mode=app ;;
  backend | --backend) mode=backend ;;
  -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown option '$mode' (try --help)" ;;
esac

command -v uv >/dev/null || die "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/"

say "Backend: installing dependencies"
(cd "$root/backend" && uv sync --frozen --quiet)

if [[ ! -f "$root/backend/.env" ]]; then
  cp "$root/backend/.env.example" "$root/backend/.env"
  say "Created backend/.env from .env.example — put your OPENROUTER_API_KEY there for Ask (never commit it)"
elif ! grep -Eq '^OPENROUTER_API_KEY=.+' "$root/backend/.env"; then
  say "No OPENROUTER_API_KEY in backend/.env: everything works except Ask"
fi

converter="$root/tools/fortress-onenote/target/release/fortress-onenote"
if [[ ! -x "$converter" ]] && command -v cargo >/dev/null; then
  say "Building the OneNote converter (first run only)"
  (cd "$root/tools/fortress-onenote" && cargo build --release --locked --quiet) \
    || say "Converter build failed; everything except OneNote import still works"
fi

if [[ "$mode" == backend ]]; then
  say "Backend on http://127.0.0.1:8000 (Ctrl-C to stop)"
  cd "$root/backend"
  exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
fi

[[ "$(uname -s)" == Darwin ]] || die "the app needs macOS; use './start.sh --backend' to run only the backend"
command -v swift >/dev/null || die "swift is not installed: install Xcode 26 from the App Store"

say "Building and opening the Mac app (it starts and stops the backend itself)"
cd "$root/mac"
exec swift run --configuration release FortressNotes
