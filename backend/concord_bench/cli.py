"""`python -m concord_bench.cli <command>`: migrate, revision, record, and the offline kit commands
export-offline, verify-kit and add-kit (OFFLINE_EXPORT_PLAN.md)."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concord-bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create data folders and bring the database to the latest schema")
    rv = sub.add_parser("revision", help="autogenerate an Alembic revision from model changes")
    rv.add_argument("message")
    rec = sub.add_parser("record", help="re-record adapter responses on the fixture set against live services")
    rec.add_argument("--out", default="fixtures/recorded")
    rec.add_argument("--services", nargs="*", default=None, help="limit to these benchmark models")
    rec.add_argument("--extra-checks", nargs="*", default=None, help="also record compatibility checks of these")
    ex = sub.add_parser("export-offline", help="write an offline kit of these platform services into a folder")
    ex.add_argument("names", nargs="+", help="platform service names or aliases")
    ex.add_argument("--kind", choices=["full", "models"], default="full",
                    help="full: Concord + platform + models (sets up a machine); models: models only (default full)")
    ex.add_argument("--out", help="folder the kit is written into (default: CONCORD_DATA/offline)")
    ex.add_argument("--name", help="kit name (default: from the services)")
    ex.add_argument("--app", action="append", metavar="APP[=IMAGE]",
                    help="an app a full kit carries: concord-bench or concord-label, with its image (default: "
                         "CONCORD_IMAGE for this app, the contract name for the other); repeat for both "
                         "(default: this app only)")
    vk = sub.add_parser("verify-kit", help="check an offline kit's files against its manifest.json")
    vk.add_argument("kit")
    ak = sub.add_parser("add-kit", help="merge a model kit into an offline install (run by install.sh --add)")
    ak.add_argument("kit")
    ak.add_argument("install")
    ak.add_argument("--replace", action="store_true", help="take the kit's image where the install has another")
    ak.add_argument("--check", action="store_true", help="only check the merge rules; change nothing")
    args = ap.parse_args(argv)

    if args.cmd in ("export-offline", "verify-kit", "add-kit"):
        return offline(args)

    from .core import db

    if args.cmd == "migrate":
        db.migrate()
        print("database at", db.db_url())
    elif args.cmd == "revision":
        from alembic import command

        db.migrate()
        command.revision(db.alembic_config(), message=args.message, autogenerate=True)
    elif args.cmd == "record":
        from .recording import record

        return record(args.out, args.services, args.extra_checks)
    return 0


def offline(args: argparse.Namespace) -> int:
    """The offline kit commands. They touch no database: add-kit runs on the offline machine as a user."""
    from pathlib import Path

    if args.cmd == "export-offline":
        from .core import paths
        from .offline.export import ExportError, export

        dest = Path(args.out).expanduser().resolve() if args.out else paths.offline_kits()
        apps = None
        if args.app:
            apps = {}
            for a in args.app:
                app, _, image = a.partition("=")
                apps[app] = image or None
        try:
            out = export(args.names, args.kind, dest, apps, args.name)
        except ExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(out["path"])
    elif args.cmd == "verify-kit":
        from .offline.kit import KitError, verify

        try:
            problems = verify(Path(args.kit))
        except KitError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for p in problems:
            print(p)
        print(f"{len(problems)} problem(s)" if problems else f"ok: {args.kit} matches its manifest")
        return 1 if problems else 0
    else:
        from .offline.kit import KitError
        from .offline.merge import MergeError, add_kit

        try:
            add_kit(Path(args.kit), Path(args.install), args.replace, args.check)
        except (KitError, MergeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
