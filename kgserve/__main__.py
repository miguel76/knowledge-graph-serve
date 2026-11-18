"""Command line interface: ``python -m kgserve <command>``."""

from __future__ import annotations

import argparse
import logging

from . import __version__, rdffiles
from .zenodo import Zenodo


def cmd_sync(args: argparse.Namespace) -> None:
    from .config import Config
    from .sync import Syncer

    Syncer(Config.load(args.config)).run(once=args.once, force=args.force)


def cmd_ld(args: argparse.Namespace) -> None:
    from .config import Config
    from .ld.server import serve

    serve(Config.load(args.config), port=args.port)


def cmd_resolve(args: argparse.Namespace) -> None:
    record = Zenodo(args.api).record(args.record, not args.no_follow)
    print(f"{record.title}\n  version {record.version or '-'} ({record.publication_date}), {record.doi}"
          f"\n  concept {record.conceptdoi}, license {record.license or '-'}\n  {record.html_url}")
    loadable = set(rdffiles.select([f.key for f in record.files], [], []))
    for f in record.files:
        print(f"  {'*' if f.key in loadable else ' '} {f.key:40} {f.size / 1e6:10.1f} MB")
    print("  (* = loaded by default)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kgserve", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="keep the triple store in sync with the Zenodo record")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--force", action="store_true", help="reload even if the store is up to date")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ld", help="run the linked-data server")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_ld)

    for p in (sub.choices["sync"], sub.choices["ld"]):
        p.add_argument("--config", help="path of config.json (default: $KG_CONFIG or /config/config.json)")

    p = sub.add_parser("resolve", help="show the latest version of a Zenodo record and its files")
    p.add_argument("record", help="Zenodo record id, DOI or URL")
    p.add_argument("--api", default="https://zenodo.org/api")
    p.add_argument("--no-follow", action="store_true", help="do not follow to the latest version")
    p.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
