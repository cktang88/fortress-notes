#!/usr/bin/env bash
# Upgrade a notes database older than the current schema, once.
#
#   ./upgrade-database.sh [path/to/.fortress.sqlite3]   (default: notes/.fortress.sqlite3)
#
# Quit Fortress first. A copy of the database is kept next to it as
# .fortress.sqlite3.before-upgrade. The upgrade code lives on the
# 'last-migrating-release' branch; this runs it from a temporary checkout with the
# backend's Python environment, then removes the checkout.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
arg="${1:-$root/notes/.fortress.sqlite3}"
db="$(cd "$(dirname "$arg")" && pwd)/$(basename "$arg")"
[[ -f "$db" ]] || { echo "error: no database at $db" >&2; exit 1; }

python="${FORTRESS_PYTHON:-$root/backend/.venv/bin/python}"
[[ -x "$python" ]] || (cd "$root/backend" && uv sync --frozen --quiet)

cp "$db" "$db.before-upgrade"
echo "Backed up to $db.before-upgrade"

checkout="$(mktemp -d)/fortress-upgrade"
git -C "$root" fetch --quiet origin last-migrating-release
git -C "$root" worktree add --quiet --detach "$checkout" FETCH_HEAD
trap 'git -C "$root" worktree remove --force "$checkout"' EXIT

(cd "$checkout/backend" && "$python" -c "
import sys
from pathlib import Path
from app import block_store
block_store.initialize(Path(sys.argv[1]))
" "$db")
echo "Upgraded $db. Open Fortress again."
