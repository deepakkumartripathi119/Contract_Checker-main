"""Claim one job at a time and run the checker. This process has no HTTP port."""

from __future__ import annotations

import logging
import os
import time

from core.jobs import claim_next, mark_failed, mark_succeeded
from scripts.prepare_runtime import prepare
from services.contract_check import run_job

log = logging.getLogger("worker")


def run_once() -> bool:
    """Claim one queued job. Return False when the queue is empty."""
    job = claim_next()
    if job is None:
        return False
    try:
        result = run_job(job)
    except Exception as exc:
        log.exception("job %s failed", job["job_id"])
        mark_failed(job["job_id"], str(exc))
        return True
    mark_succeeded(job["job_id"], result)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    prepare()
    wait = float(os.getenv("JOB_POLL_SECONDS", "1"))
    while True:
        if not run_once():
            time.sleep(wait)


if __name__ == "__main__":
    main()
