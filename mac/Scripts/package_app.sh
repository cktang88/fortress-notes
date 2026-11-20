#!/bin/bash
# Builds "Fortress Notes.app" from the Swift package: release build, bundle layout,
# the String Catalog compiled into the app's resources, and a Hardened Runtime signature
# (Developer ID, not sandboxed, no runtime exceptions: see Packaging/FortressNotes.entitlements).
#
#   Scripts/package_app.sh [output-dir]
#
# SIGN_IDENTITY   "Developer ID Application: …" for a distributable build; default "-"
#                 (ad hoc: runs on this Mac only, what CI checks).
# NOTARY_PROFILE  a `xcrun notarytool store-credentials` profile: when set (and signed with
#                 a Developer ID), the app is notarized and the ticket stapled.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-.build/app}"
IDENTITY="${SIGN_IDENTITY:--}"
APP="$OUT/Fortress Notes.app"

swift build -c release --product FortressNotes
BIN="$(swift build -c release --show-bin-path)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/FortressNotes" "$APP/Contents/MacOS/FortressNotes"
cp Packaging/Info.plist "$APP/Contents/Info.plist"
# SwiftPM resource bundles (none today; copied if a target gains resources).
for bundle in "$BIN"/*.bundle; do
  [ -e "$bundle" ] && cp -R "$bundle" "$APP/Contents/Resources/"
done
# Text("…") and String(localized:) look in the main bundle: the catalog goes there.
xcrun xcstringstool compile Packaging/Localizable.xcstrings --output-directory "$APP/Contents/Resources"

sign=(codesign --force --options runtime --entitlements Packaging/FortressNotes.entitlements --sign "$IDENTITY")
[ "$IDENTITY" != "-" ] && sign+=(--timestamp)
"${sign[@]}" "$APP"
codesign --verify --strict --verbose=2 "$APP"
# The Hardened Runtime flag must be on the signature.
codesign --display --verbose=2 "$APP" 2>&1 | grep -q "flags=.*runtime" || { echo "error: hardened runtime missing" >&2; exit 1; }

if [ -n "${NOTARY_PROFILE:-}" ] && [ "$IDENTITY" != "-" ]; then
  ditto -c -k --keepParent "$APP" "$OUT/FortressNotes.zip"
  xcrun notarytool submit "$OUT/FortressNotes.zip" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  spctl --assess --type execute --verbose "$APP"
fi
echo "$APP"
