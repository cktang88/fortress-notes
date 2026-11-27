#!/usr/bin/env python3
"""Check the dependency cooldown (docs/dependencies.md): everything we pin was
released at least MIN_AGE_DAYS before the commit that pinned it.

* backend/uv.lock and tools/fortress-onenote-graph/uv.lock: the upload time of every
  sdist and wheel (recorded in the lock).
* tools/fortress-onenote/Cargo.lock: every crates.io version's publish time (the
  sparse index's ``pubtime``), and its checksum against the index.
* tools/fortress-onenote/rust-toolchain.toml: the Rust release's date.
* .github/workflows/*.yml: every ``uses:`` is a full commit SHA with a ``# vX.Y.Z``
  comment, that tag points to that SHA, and its release is old enough; the uv
  version setup-uv installs too.
* The model revisions (ColBERT in backend/app/config.py, the picture describer in
  backend/app/captions.py): each commit's date on the Hub.
* mac/Package.swift: no package dependencies (add them to this check first).

"Pinned" is the last commit that changed the file (now, for uncommitted changes),
so CI needs the full history (actions/checkout with fetch-depth: 0). Standard
library only; run from anywhere: ``python3 .github/scripts/check_cooldown.py``.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

MIN_AGE_DAYS = 7
ROOT = Path(__file__).resolve().parents[2]
# Every uv project: (pyproject.toml, uv.lock).
UV_PROJECTS = [
    (ROOT / "backend/pyproject.toml", ROOT / "backend/uv.lock"),
    (ROOT / "tools/fortress-onenote-graph/pyproject.toml", ROOT / "tools/fortress-onenote-graph/uv.lock"),
]
CARGO_LOCK = ROOT / "tools/fortress-onenote/Cargo.lock"
RUST_TOOLCHAIN = ROOT / "tools/fortress-onenote/rust-toolchain.toml"
# Hugging Face models: (file, the variable naming the repository, the one pinning its commit).
MODELS = [
    (ROOT / "backend/app/config.py", "COLBERT_MODEL", "COLBERT_REVISION"),
    (ROOT / "backend/app/captions.py", "MODEL_REPO", "MODEL_REVISION"),
]
PACKAGE_SWIFT = ROOT / "mac/Package.swift"
WORKFLOWS = ROOT / ".github/workflows"
USER_AGENT = "fortress-notes-cooldown-check"

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)
    print(f"FAIL {message}")


def parse_time(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def pinned_at(path: Path) -> dt.datetime:
    """When ``path`` was last changed: its last commit, or now if it has changes."""

    relative = str(path.relative_to(ROOT))
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", relative],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not dirty:
        stamp = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%cI", "--", relative],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if stamp:
            return parse_time(stamp)
    return dt.datetime.now(dt.UTC)


def cutoff_for(path: Path) -> dt.datetime:
    return pinned_at(path) - dt.timedelta(days=MIN_AGE_DAYS)


def fetch(url: str, *, token: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_json(url: str, **kwargs):
    return json.loads(fetch(url, **kwargs))


# --- Python (uv) -------------------------------------------------------------------------


def check_uv() -> None:
    for pyproject, lockfile in UV_PROJECTS:
        check_uv_project(pyproject, lockfile)


def check_uv_project(pyproject: Path, lockfile: Path) -> None:
    name = lockfile.relative_to(ROOT)
    settings = tomllib.loads(pyproject.read_text()).get("tool", {}).get("uv", {})
    if not settings.get("exclude-newer"):
        fail(f"{pyproject.relative_to(ROOT)}: [tool.uv] exclude-newer is not set")
    cutoff = cutoff_for(lockfile)
    lock = tomllib.loads(lockfile.read_text())
    files = 0
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue  # the project itself, or a path/git source (none today)
        artifacts = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
        for artifact in artifacts:
            files += 1
            uploaded = artifact.get("upload-time")
            if uploaded is None:
                fail(f"{name}: {package['name']} {package['version']}: no upload time for "
                     f"{artifact['url'].rsplit('/', 1)[-1]}")
            elif parse_time(uploaded) > cutoff:
                fail(f"{name}: {package['name']} {package['version']}: "
                     f"{artifact['url'].rsplit('/', 1)[-1]} uploaded {uploaded}, after {cutoff:%Y-%m-%dT%H:%MZ}")
    print(f"ok   {name}: {files} files of {len(lock['package'])} packages, cutoff {cutoff:%Y-%m-%d %H:%M} UTC")


# --- Rust (cargo) ------------------------------------------------------------------------


def _index_path(name: str) -> str:
    name = name.lower()
    if len(name) <= 2:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def check_cargo() -> None:
    cutoff = cutoff_for(CARGO_LOCK)
    lock = tomllib.loads(CARGO_LOCK.read_text())
    checked = 0
    for package in lock["package"]:
        source = package.get("source", "")
        if not source:
            continue  # this crate, or a vendored [patch] (path) crate
        if source != "registry+https://github.com/rust-lang/crates.io-index":
            fail(f"Cargo.lock: {package['name']} {package['version']}: unsupported source {source}")
            continue
        index = fetch(f"https://index.crates.io/{_index_path(package['name'])}").decode()
        entry = next(
            (e for e in map(json.loads, index.splitlines()) if e["vers"] == package["version"]), None
        )
        if entry is None:
            fail(f"Cargo.lock: {package['name']} {package['version']}: not in the crates.io index")
            continue
        checked += 1
        if entry["cksum"] != package.get("checksum"):
            fail(f"Cargo.lock: {package['name']} {package['version']}: checksum differs from the index")
        published = entry.get("pubtime")
        if published is None:
            fail(f"Cargo.lock: {package['name']} {package['version']}: no publish time in the index")
        elif parse_time(published) > cutoff:
            fail(f"Cargo.lock: {package['name']} {package['version']}: published {published}, "
                 f"after {cutoff:%Y-%m-%dT%H:%MZ}")
    print(f"ok   Cargo.lock: {checked} crates, cutoff {cutoff:%Y-%m-%d %H:%M} UTC")


def check_rust_toolchain() -> None:
    channel = tomllib.loads(RUST_TOOLCHAIN.read_text())["toolchain"]["channel"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", channel):
        fail(f"rust-toolchain.toml: channel {channel!r} is not an exact release (e.g. 1.99.0)")
        return
    manifest = fetch(f"https://static.rust-lang.org/dist/channel-rust-{channel}.toml").decode()
    released = parse_time(tomllib.loads(manifest)["date"] + "T23:59:59Z")
    cutoff = cutoff_for(RUST_TOOLCHAIN)
    if released > cutoff:
        fail(f"rust-toolchain.toml: Rust {channel} released {released:%Y-%m-%d}, after {cutoff:%Y-%m-%d}")
    else:
        print(f"ok   Rust {channel} (released {released:%Y-%m-%d})")


# --- GitHub Actions ------------------------------------------------------------------------

USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?:\s*#\s*(?P<comment>\S+))?", re.MULTILINE)
SETUP_UV_VERSION = re.compile(
    r"uses:\s*astral-sh/setup-uv@\S+.*?\n(?:\s+.*\n)*?\s+version:\s*\"?(?P<version>[^\"\s]+)\"?"
)


@functools.cache
def _tag_on_github(repo: str, tag: str) -> tuple[str, dt.datetime]:
    """The commit ``tag`` points to and when it was released (the GitHub release's
    publish time, else the tag's date), through the API in CI, else through git."""

    token = os.environ.get("GITHUB_TOKEN")
    api = f"https://api.github.com/repos/{repo}"
    try:
        ref = fetch_json(f"{api}/git/ref/tags/{tag}", token=token)["object"]
        while ref["type"] == "tag":
            tag_object = fetch_json(f"{api}/git/tags/{ref['sha']}", token=token)
            ref = tag_object["object"]
        try:
            release = fetch_json(f"{api}/releases/tags/{tag}", token=token)
            return ref["sha"], parse_time(release["published_at"])
        except urllib.error.HTTPError:
            commit = fetch_json(f"{api}/git/commits/{ref['sha']}", token=token)
            return ref["sha"], parse_time(commit["committer"]["date"])
    except (urllib.error.URLError, KeyError) as error:
        if os.environ.get("GITHUB_ACTIONS"):
            raise
        print(f"note GitHub API unavailable for {repo} ({error}); reading the tag with git")
    with tempfile.TemporaryDirectory() as directory:
        git = ["git", "-C", directory]
        subprocess.run(["git", "init", "-q", "--bare", directory], check=True)
        subprocess.run(
            git + ["fetch", "-q", "--depth=1", "--filter=blob:none",
                   f"https://github.com/{repo}", f"refs/tags/{tag}:refs/tags/{tag}"],
            check=True,
        )
        line = subprocess.run(
            git + ["for-each-ref", "--format=%(creatordate:iso-strict) %(objectname) %(*objectname)",
                   f"refs/tags/{tag}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        return (line[2] if len(line) > 2 else line[1]), parse_time(line[0])


def check_actions() -> None:
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        text = workflow.read_text()
        cutoff = cutoff_for(workflow)
        name = workflow.relative_to(ROOT)
        for match in USES.finditer(text):
            ref, comment = match["ref"], match["comment"]
            if ref.startswith("./") or ref.startswith("docker://"):
                if ref.startswith("docker://") and "@sha256:" not in ref:
                    fail(f"{name}: {ref}: pin the image by digest")
                continue
            action, _, sha = ref.partition("@")
            repo = "/".join(action.split("/")[:2])
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                fail(f"{name}: {ref}: not pinned to a full commit SHA")
                continue
            if not comment or not re.fullmatch(r"v\d+(\.\d+)*", comment):
                fail(f"{name}: {ref}: needs a '# vX.Y.Z' comment naming its release")
                continue
            tagged, released = _tag_on_github(repo, comment)
            if tagged != sha:
                fail(f"{name}: {action}@{sha[:12]}: tag {comment} points to {tagged[:12]}")
            elif released > cutoff:
                fail(f"{name}: {action} {comment} released {released:%Y-%m-%d}, after {cutoff:%Y-%m-%d}")
            else:
                print(f"ok   {name}: {action} {comment} (released {released:%Y-%m-%d})")
        for match in SETUP_UV_VERSION.finditer(text):
            version = match["version"]
            files = fetch_json(f"https://pypi.org/pypi/uv/{version}/json")["urls"]
            uploaded = min(parse_time(f["upload_time_iso_8601"]) for f in files)
            if uploaded > cutoff:
                fail(f"{name}: uv {version} released {uploaded:%Y-%m-%d}, after {cutoff:%Y-%m-%d}")
            else:
                print(f"ok   {name}: uv {version} (released {uploaded:%Y-%m-%d})")
        # Tools run with uvx (ruff): an exact version, old enough.
        commands = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for match in re.finditer(r"\buvx\s+(?P<tool>[A-Za-z0-9_.-]+)(?P<pin>@\S+)?", commands):
            tool, pin = match["tool"], (match["pin"] or "")[1:]
            if not re.fullmatch(r"\d+(\.\d+)*", pin):
                fail(f"{name}: uvx {tool}: pin an exact version (uvx {tool}@X.Y.Z)")
                continue
            files = fetch_json(f"https://pypi.org/pypi/{tool}/{pin}/json")["urls"]
            uploaded = min(parse_time(f["upload_time_iso_8601"]) for f in files)
            if uploaded > cutoff:
                fail(f"{name}: {tool} {pin} released {uploaded:%Y-%m-%d}, after {cutoff:%Y-%m-%d}")
            else:
                print(f"ok   {name}: {tool} {pin} (released {uploaded:%Y-%m-%d})")
        if re.search(r"astral-sh/setup-uv@", text) and not SETUP_UV_VERSION.search(text):
            fail(f"{name}: setup-uv without an exact `version:` installs the newest uv")


# --- Hugging Face model, SwiftPM -------------------------------------------------------------


def check_model() -> None:
    for path, name_var, revision_var in MODELS:
        if not path.exists():
            continue  # an optional module that was removed
        text = path.read_text()
        model = re.search(rf'^{name_var} = "([^"]+)"', text, re.MULTILINE)
        revision = re.search(rf'^{revision_var} = "([0-9a-f]{{40}})"', text, re.MULTILINE)
        where = path.relative_to(ROOT)
        if not model or not revision:
            fail(f"{where}: {name_var} and a 40-hex {revision_var} are required")
            continue
        info = fetch_json(f"https://huggingface.co/api/models/{model[1]}/revision/{revision[1]}")
        if info.get("sha") != revision[1]:
            fail(f"{model[1]}: revision {revision[1]} not found on the Hub")
            continue
        cutoff = cutoff_for(path)
        committed = parse_time(info["lastModified"])
        if committed > cutoff:
            fail(f"{model[1]}@{revision[1][:12]}: committed {committed:%Y-%m-%d}, after {cutoff:%Y-%m-%d}")
        else:
            print(f"ok   {model[1]}@{revision[1][:12]} (committed {committed:%Y-%m-%d})")


def check_swift() -> None:
    if re.search(r"\.package\s*\(", PACKAGE_SWIFT.read_text()):
        fail("mac/Package.swift: package dependencies are not covered by this check yet "
             "(pin with exact: and commit Package.resolved; see docs/dependencies.md)")
    else:
        print("ok   mac/Package.swift: no package dependencies")


def main() -> int:
    for check in (check_uv, check_cargo, check_rust_toolchain, check_actions, check_model, check_swift):
        try:
            check()
        except Exception as error:  # noqa: BLE001 - report every check
            fail(f"{check.__name__}: {type(error).__name__}: {error}")
    if errors:
        print(f"\n{len(errors)} problem(s); see docs/dependencies.md", file=sys.stderr)
        return 1
    print(f"\nEverything pinned is at least {MIN_AGE_DAYS} days older than its pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
