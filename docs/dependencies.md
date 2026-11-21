# Dependency policy

**Rule.** Every third-party thing we pull — Python packages, Rust crates, the Rust
toolchain, uv, GitHub Actions, the ColBERT model — is pinned to an exact release
that was **at least 7 days old when we pinned it**. A compromised or broken
release is usually found and yanked within days; we let others find it first.
Automated update pull requests wait **14 days**. Security fixes may skip the wait,
by a deliberate, reviewed exception (below).

The rule is enforced by tooling, not by memory:

| What | Pinned in | Cooldown enforced by |
|---|---|---|
| Python packages | `backend/uv.lock` and `tools/fortress-onenote-graph/uv.lock` (hashes, upload times), installed with `uv sync --frozen` / `uv run --frozen` everywhere (CI, `start.sh`, the Mac app's `uv run --frozen` fallback) | `[tool.uv] exclude-newer = "7 days"` in each `pyproject.toml`: `uv lock` never considers a file uploaded in the last 7 days. CI re-checks every sdist and wheel. |
| OCR models (PP-OCRv6) | inside the `rapidocr` wheel, whose hash is in `uv.lock`; rapidocr checks each model file's SHA-256 | as Python packages |
| ColBERT model | `COLBERT_REVISION` (a commit of `lightonai/Agent-ModernColBERT`) in `backend/app/config.py`; PyLate, the tokenizer and the MLX encoder load that commit | CI checks the commit exists and its date |
| Rust crates | `tools/fortress-onenote/Cargo.lock`, built with `--locked` everywhere | CI checks every crate version's publish time (crates.io index `pubtime`) and checksum |
| Rust toolchain | `tools/fortress-onenote/rust-toolchain.toml` (exact version) | CI checks the release date |
| uv | `version:` of `astral-sh/setup-uv` in the workflows; `required-version` in `pyproject.toml` is only a floor | CI checks the release date |
| Tools run with `uvx` in CI (ruff) | the exact version in the workflow (`uvx ruff@X.Y.Z`) | CI checks the PyPI release date |
| GitHub Actions | every `uses:` is a full commit SHA with a `# vX.Y.Z` comment | CI checks the tag points to that SHA and the release date |
| Swift packages | none: `mac/Package.swift` has no dependencies | CI fails if one is added before this policy covers it |

The check is [`.github/scripts/check_cooldown.py`](../.github/scripts/check_cooldown.py)
(workflow *Dependency cooldown*, standard library only). It dates each pin by the
last commit that changed the file and fails if anything in it was released less
than 7 days before. Run it locally before pushing an update:

```sh
python3 .github/scripts/check_cooldown.py
```

[Dependabot](../.github/dependabot.yml) proposes updates weekly for uv, cargo, the
Rust toolchain and GitHub Actions, with `cooldown: default-days: 14` (it keeps SHA
pins and updates their version comments).

## Updating

**Python.** `cd backend && uv lock --upgrade` (or `--upgrade-package NAME`), then
`uv sync --frozen` and the tests. uv computes the cutoff as "now minus 7 days" at
each resolution; the span (not the moment) is recorded in `uv.lock`, and a lock
that already satisfies the project is never changed by time passing. Use uv ≥
0.12.12 to lock: older uv can keep a wheel added to an older release after the
cutoff (CI would then fail). Read the changelog of every package whose major or
minor version moves.

**Rust.** Cargo 1.99 has no stable cooldown setting, so generate the lock with an
explicit cutoff:

```sh
cd tools/fortress-onenote
RUSTC_BOOTSTRAP=1 cargo generate-lockfile -Zunstable-options \
  --publish-time "$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)"
```

(on macOS: `date -u -v-7d +%Y-%m-%dT%H:%M:%SZ`; `--publish-time` is Cargo's unstable `lockfile-publish-time`; `RUSTC_BOOTSTRAP=1`
lets this one command use it on the pinned stable toolchain.) Once
`rust-toolchain.toml` is at 1.100 or newer, add the now-stable setting instead and
plain `cargo update` respects it:

```toml
# tools/fortress-onenote/.cargo/config.toml
[registry]
global-min-publish-age = "7 days"

[resolver]
incompatible-publish-age = "deny"
```

(Cargo 1.99 warns about and ignores these keys, which is why they are not there yet.)
`onenote_parser` and `cab` are vendored with patches (`vendor/PATCHES.md`):
updating them means re-applying those patches.

**Rust toolchain.** Set `channel` in `rust-toolchain.toml` to a release at least 7
days old (dates: `https://static.rust-lang.org/dist/channel-rust-<version>.toml`).

**GitHub Actions.** Pin the commit the release tag points to, with the tag as a
comment: `uses: owner/action@<40-hex sha> # v1.2.3`
(`git ls-remote --tags https://github.com/owner/action`; for an annotated tag use
the `^{}` line). Read the release notes for every major version. Bump the uv
`version:` the same way (its PyPI upload date counts).

**ColBERT model.** Pick a commit of the model repository at least 7 days old
(`https://huggingface.co/api/models/<model>/commits/main`), read what changed, set
`COLBERT_REVISION`, and run the real-model tests (`FORTRESS_REAL_MODEL=1
FORTRESS_REAL_COLBERT=1`, and the macOS CI job). The embedding store is keyed by
model and revision, so a new revision re-indexes every note in the background.
Switching to another model also needs `app/relevance.py`'s calibration
(`scripts/calibrate_related.py`). A different `COLBERT_MODEL` set in the
environment is pinned only if `COLBERT_REVISION` is set with it.

**Exceptions.** A security fix needed sooner than 7 days goes in on purpose: for
uv, `exclude-newer-package = { name = "<RFC 3339 time after the fix>" }` in
`[tool.uv]`; for Cargo, `cargo update --precise`; then the CI check fails for that
pin, which is the reminder to document the exception in the pull request and to
remove the override once the release is 7 days old.
