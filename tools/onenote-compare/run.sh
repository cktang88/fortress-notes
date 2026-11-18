#!/usr/bin/env bash
# Compare a OneNote export with what Fortress imports from it.
#
#   tools/onenote-compare/run.sh <export.zip | notebook folder | file.one | file.onepkg>... [-o REPORT_DIR]
#   tools/onenote-compare/run.sh --each <folder of .one/.onepkg/.zip files> -o REPORT_DIR [--shots failing --sample 20] [--skip TEXT]
#
# Builds the converter (release), installs one2html (MIT) into a cache folder if
# it is not on PATH, then runs compare.py in the backend's uv environment with
# Playwright. Opens nothing; prints the report path. See README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CACHE="${FORTRESS_COMPARE_CACHE:-$HOME/.cache/fortress-onenote-compare}"
OUT=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--out) OUT="$2"; shift 2 ;;
    --each) ARGS+=("--each" "$(realpath "$2")"); shift 2 ;;
    --shots|--sample|--skip) ARGS+=("$1" "$2"); shift 2 ;;
    --prune) ARGS+=("$1"); shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) ARGS+=("$(realpath "$1")"); shift ;;
  esac
done
[[ ${#ARGS[@]} -gt 0 ]] || { echo "usage: $0 <input>... [-o REPORT_DIR]" >&2; exit 2; }
OUT="$(realpath -m "${OUT:-$PWD/onenote-compare-report}")"

# 1. Converter.
CONVERTER="${FORTRESS_ONENOTE_BIN:-$REPO/tools/fortress-onenote/target/release/fortress-onenote}"
if [[ -z "${FORTRESS_ONENOTE_BIN:-}" ]]; then
  echo "building fortress-onenote (release)…" >&2
  (cd "$REPO/tools/fortress-onenote" && cargo build --release --quiet)
fi

# 2. one2html (reference renderer). Its default "backtrace" feature needs nightly Rust.
ONE2HTML="${ONE2HTML:-$(command -v one2html || true)}"
if [[ -z "$ONE2HTML" ]]; then
  ONE2HTML="$CACHE/bin/one2html"
  if [[ ! -x "$ONE2HTML" ]]; then
    echo "installing one2html into $CACHE…" >&2
    cargo install --locked one2html@2.0.1 --no-default-features --features bin,native-fs,onepkg \
      --root "$CACHE" --quiet
  fi
fi

# 3. Compare (backend environment + Playwright; Chromium from PLAYWRIGHT_BROWSERS_PATH or `playwright install chromium`).
cd "$REPO/backend"
PYTHONPATH="$REPO/backend" uv run --frozen --quiet --with playwright==1.63.0 \
  python "$HERE/compare.py" --converter "$CONVERTER" --one2html "$ONE2HTML" --out "$OUT" "${ARGS[@]}"
