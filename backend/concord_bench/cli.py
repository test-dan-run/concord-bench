"""`python -m concord_bench.cli <command>`: migrate, revision, record and move-originals."""

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
    sub.add_parser("move-originals", help="upload data/originals to the Dataset Catalog's store, hold them, and rename "
                                          "the folder data/originals.moved (DATASET_STORE must be set)")
    args = ap.parse_args(argv)

    from .core import db

    try:
        if args.cmd in ("migrate", "revision", "move-originals"):
            db.migrate()
    except db.ForeignDatabase as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "migrate":
        print("database at", db.db_url())
    elif args.cmd == "revision":
        from alembic import command

        command.revision(db.alembic_config(), message=args.message, autogenerate=True, rev_id=db.next_revision_id())
    elif args.cmd == "record":
        from .recording import record

        return record(args.out, args.services)
    elif args.cmd == "move-originals":
        from .core.db import session_scope
        from .datasets.store import move_originals
        from .platform import dataset_catalog

        try:
            with session_scope() as session:
                out = move_originals(session)
        except dataset_catalog.Unavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{out['files']} originals: {out['uploaded']} uploaded, {out['already_in_store']} already in the store, "
              f"{out['held']} held, {out['not_used']} used by no dataset (not held)"
              + (f", {len(out['skipped'])} skipped (not named by a sha256)" if out["skipped"] else ""))
        return 1 if out["failed"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
