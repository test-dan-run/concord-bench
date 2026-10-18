"""`python -m concord.cli <command>`: migrate, revision, record."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concord")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create data folders and bring the database to the latest schema")
    rv = sub.add_parser("revision", help="autogenerate an Alembic revision from model changes")
    rv.add_argument("message")
    rec = sub.add_parser("record", help="re-record adapter responses on the fixture set against live services")
    rec.add_argument("--out", default="fixtures/recorded")
    rec.add_argument("--services", nargs="*", default=None, help="limit to these benchmark models")
    rec.add_argument("--extra-checks", nargs="*", default=None, help="also record compatibility checks of these")
    args = ap.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
