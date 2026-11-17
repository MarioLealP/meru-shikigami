"""Temporal's time-skipping test server for the workflow tests, started with a retry.

temporalio downloads the test server binary the first time it is asked (the CI job's HOME is a fresh /tmp, so that is
every run) and the download sometimes arrives broken (cm-sg3b, cm-gt5i). A broken archive is not kept: every attempt
downloads into its own fresh directory, and a failed start is tried again a few times before the test errors.
"""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

ATTEMPTS = 3
PAUSE = 5.0  # seconds between attempts


async def start_with_retry[T](
    start: Callable[[str], Awaitable[T]], attempts: int = ATTEMPTS, pause: float = PAUSE
) -> T:
    """`start(download_dir)`, again with a new empty download_dir after a RuntimeError (what temporalio raises)."""
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory(prefix="temporal-test-server-") as download_dir:
            try:
                return await start(download_dir)
            except RuntimeError:
                if attempt == attempts:
                    raise
        await asyncio.sleep(pause)
    raise AssertionError("unreachable")


async def start_time_skipping() -> WorkflowEnvironment:
    """A started time-skipping environment; the caller closes it (`async with` on the result)."""

    def start(download_dir: str) -> Awaitable[WorkflowEnvironment]:
        return WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter, download_dest_dir=str(Path(download_dir))
        )

    return await start_with_retry(start)
