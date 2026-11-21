"""Command line: ``fortress-onenote-graph login|notebooks|download|convert|logout``.

Exit codes: 0 done; 1 error; 2 usage; 3 interrupted by throttling or the network
(run the same command again: it resumes from the cache).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .graph import GraphClient, GraphError
from .progress import Progress
from .store import Store, default_cache_dir, default_config_dir, key, read_json, write_json
from .throttle import Throttle

EXIT_RESUMABLE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fortress-onenote-graph",
        description="Download OneNote notebooks with Microsoft Graph (read-only) and write a manifest "
                    "that Fortress Notes imports (File › Import › From OneNote…).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--client-id", help="your Entra app's Application (client) ID "
                                            "(default: $FORTRESS_ONENOTE_CLIENT_ID)")
    common.add_argument("--account", choices=["personal", "work"], default="personal",
                        help="personal Microsoft account (Notes.Read) or work/school account (Notes.Read.All)")
    common.add_argument("--tenant", help="sign-in authority tenant (default: consumers for personal, "
                                         "organizations for work; or $FORTRESS_ONENOTE_TENANT)")
    common.add_argument("--device-code", action="store_true", help="sign in with a code instead of a browser")
    common.add_argument("--cache-dir", type=Path, default=None, help=f"download cache (default {default_cache_dir()})")
    common.add_argument("--config-dir", type=Path, default=None,
                        help=f"token cache location off macOS (default {default_config_dir()})")
    common.add_argument("--progress", choices=["text", "json"], default="text",
                        help="progress on stderr as text or JSON lines")
    common.add_argument("-q", "--quiet", action="store_true", help="only warnings and the summary")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", parents=[common], help="sign in and show the account")
    commands.add_parser("logout", parents=[common], help="forget the cached sign-in")
    notebooks = commands.add_parser("notebooks", parents=[common], help="list your notebooks")
    notebooks.add_argument("--json", action="store_true",
                           help='one JSON line: {"notebooks": [{"name": …, "sections": n}, …]}')
    download = commands.add_parser("download", parents=[common],
                                   help="download notebooks (only what changed) and write the manifest")
    convert = commands.add_parser("convert", parents=[common],
                                  help="write the manifest from the cache only (no network)")
    for sub in (download, convert):
        sub.add_argument("--notebook", action="append", metavar="NAME",
                         help="only this notebook (repeatable; default: all)")
        sub.add_argument("--out", type=Path, default=Path("OneNote-Graph.zip"),
                         help="a .zip to drop on Fortress's import sheet, or a folder (default OneNote-Graph.zip)")
    download.add_argument("--no-convert", action="store_true", help="only download into the cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    progress = Progress(args.progress, quiet=args.quiet)
    cache_dir = (args.cache_dir or default_cache_dir()).expanduser()
    config_dir = (args.config_dir or default_config_dir()).expanduser()
    try:
        if args.command == "convert":
            return _convert(args, cache_dir, progress, _last_store(cache_dir))
        from .auth import AuthError, Session, resolve_client_id

        try:
            client_id = resolve_client_id(args.client_id)
            session = Session(client_id, config_dir, account_type=args.account, tenant=args.tenant,
                              device_code=args.device_code, say=progress.info if args.progress == "json"
                              else lambda message: print(message, file=sys.stderr),
                              on_device_code=progress.device_code)
            if args.command == "logout":
                removed = session.logout()
                print(json.dumps({"signed_out": removed}))
                return 0
            throttle = Throttle(state_path=cache_dir / f"throttle-{key(client_id)[:16]}.json",
                                on_wait=progress.wait)
            cache_dir.mkdir(parents=True, exist_ok=True)
            client = GraphClient(session.token, throttle, log=progress.warning)
            progress.phase("signin")
            me = client.me()
        except AuthError as error:
            print(f"fortress-onenote-graph: {error}", file=sys.stderr)
            return 1
        store = Store.for_account(cache_dir, str(me.get("id")))
        write_json(store.root / "account.json", {"name": me.get("displayName"),
                                                 "user": me.get("userPrincipalName")})
        write_json(cache_dir / "last_account.json", {"key": store.root.name})
        if args.command == "login":
            print(json.dumps({"signed_in": me.get("userPrincipalName") or me.get("displayName")}))
            return 0
        if args.command == "notebooks":
            from .download import sections_of

            listed = []
            for notebook in client.notebooks():
                name = str(notebook.get("displayName"))
                listed.append({"name": name, "sections": sum(1 for _ in sections_of(notebook, [name]))})
            if args.json:
                print(json.dumps({"notebooks": listed}, ensure_ascii=False))
            else:
                for item in listed:
                    print(f"{item['name']}\t{item['sections']} section(s)")
            return 0
        from .download import Downloader

        stats = Downloader(client, store, progress).run(args.notebook)
        progress.info(f"{stats['pages_fetched']} page(s) downloaded, {stats['pages_unchanged']} unchanged, "
                      f"{stats['resources_fetched']} file(s); {stats['requests']} request(s)")
        if args.no_convert:
            print(json.dumps({"download": stats}))
            return 0
        return _convert(args, cache_dir, progress, store, stats)
    except GraphError as error:
        resumable = error.status in (0, 429) or error.status >= 500 or error.code in ("throttled", "network")
        print(f"fortress-onenote-graph: {error}"
              + ("\nNothing is lost: run the same command again to continue." if resumable else ""),
              file=sys.stderr)
        return EXIT_RESUMABLE if resumable else 1
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue.", file=sys.stderr)
        return EXIT_RESUMABLE
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - one readable line for the user
        print(f"fortress-onenote-graph: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


def _last_store(cache_dir: Path) -> Store:
    last = read_json(cache_dir / "last_account.json") or {}
    root = cache_dir / "accounts" / str(last.get("key") or "")
    if not last.get("key") or not root.is_dir():
        raise SystemExit("Nothing downloaded yet: run `fortress-onenote-graph download` first.")
    return Store(root)


def _convert(args, cache_dir: Path, progress: Progress, store: Store, stats: dict | None = None) -> int:
    from .manifest import build

    progress.phase("convert", "Writing the manifest…")
    out = args.out.expanduser().resolve()
    summary = build(store, out, notebook_names=args.notebook,
                    progress=lambda done, total, current: progress.step("convert", done, total, current),
                    warn=progress.warning if not args.quiet else None)
    if stats is not None:
        summary["download"] = stats
    progress.phase("done", f"Done: {summary['pages']} page(s) in {summary['notebooks']} notebook(s) → {out}\n"
                           "Import it in Fortress: File › Import › From OneNote…")
    print(json.dumps(summary, ensure_ascii=False))
    return 0
