"""The worker process: `python -m concord_bench.runs.worker` (or `just worker`).

Polls the job table and runs one job at a time (one GPU, one user).
"""

from __future__ import annotations

import argparse
import logging
import signal
import time

from ..core import db
from ..core.settings import get_settings
from . import jobs

log = logging.getLogger("concord_bench.worker")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="run what is queued, then exit")
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s", datefmt="%H:%M:%S")
    get_settings()
    try:
        db.migrate()
    except db.ForeignDatabase as exc:
        log.error("%s", exc)
        return 2
    with db.session_scope() as s:
        n = jobs.recover(s)
    if n:
        log.info("requeued %d interrupted job(s)", n)
    if args.once:
        log.info("ran %d job(s)", jobs.run_pending())
        return 0
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log.info("waiting for jobs (data: %s)", get_settings().concord_data)
    while not stop:
        if jobs.run_pending(max_jobs=1) == 0:
            time.sleep(args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
