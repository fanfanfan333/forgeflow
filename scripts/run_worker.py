"""Standalone queue worker (A5 slice 1).

Consumes the durable task queue without the API server — the
"hosted worker" half of A5 slice 1 (the API server only enqueues).
Run from the repo checkout:

  python scripts/run_worker.py            # loop until Ctrl-C
  python scripts/run_worker.py --once     # one pass, then exit (ops / smoke)

Settings come from the environment (``forgeflow.config.Settings``,
``QUEUE_WORKER_*``); CLI flags override them for one invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forgeflow.config import get_settings  # noqa: E402
from forgeflow.queue.worker import QueueWorker  # noqa: E402

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ForgeFlow durable queue worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass (reap + claim + execute) and exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        help="Seconds between passes (default: queue_worker_poll_seconds)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=None,
        help="Claim lease in seconds (default: queue_worker_lease_seconds)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Rows claimed per pass (default: queue_worker_batch_size)",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
        help="Claim identity (default: worker-<random>)",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    worker = QueueWorker(
        poll_seconds=args.poll_seconds or settings.queue_worker_poll_seconds,
        lease_seconds=args.lease_seconds or settings.queue_worker_lease_seconds,
        batch_size=args.batch_size or settings.queue_worker_batch_size,
        worker_id=args.worker_id,
    )
    if args.once:
        report = await worker.run_once()
        logger.info("worker --once pass: %s", report.to_dict())
        if worker.scheduler_passes:
            logger.info("worker --once scheduler: %s", worker.scheduler_passes[-1].to_dict())
        return 0
    worker.start()
    try:
        # The loop lives on the worker's task; wait for it (it only
        # ends on cancellation, e.g. Ctrl-C → KeyboardInterrupt).
        while worker._task is not None:  # noqa: SLF001 — CLI owns the lifecycle
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await worker.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
