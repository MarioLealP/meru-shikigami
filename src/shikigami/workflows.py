"""Temporal workflows (D65, D75). Workflow code is replayed from history after a restart, so it must stay
deterministic: no I/O, clock or randomness here - that all lives in the activities."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from shikigami.activities import Activities
    from shikigami.models import AppAction
    from shikigami.plan import plan

SYNC_ID = "requests-sync"  # one id: pings while a run is open only queue one more pass (D65)
TIMEOUT = timedelta(minutes=2)  # per attempt
RETRY = RetryPolicy(maximum_attempts=5)  # then the run fails and the next ping or night tries again


@workflow.defn
class AppWorkflow:
    """One app's step, as a child of requests-sync, so many apps run side by side and fail apart (D75)."""

    @workflow.run
    async def run(self, action: AppAction) -> None:
        await workflow.execute_activity_method(
            Activities.dry_run, action, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )


@workflow.defn
class RequestsSync:
    """Reads the requests and what exists, works out the difference and hands each app to its own child workflow.
    A `poke` while running means "look again": the run makes one more pass before it ends."""

    def __init__(self) -> None:
        self._poked = False

    @workflow.signal
    def poke(self) -> None:
        self._poked = True

    @workflow.run
    async def run(self) -> list[AppAction]:
        while True:
            self._poked = False
            snapshot = await workflow.execute_activity_method(
                Activities.read_requests, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
            )
            if snapshot.problems:
                # The gate (Xolotl, D69) should never let this happen; the backstop is to act on nothing.
                workflow.logger.warning(
                    "requests at %s have problems, doing nothing: %s", snapshot.commit, snapshot.problems
                )
                actions: list[AppAction] = []
            else:
                existing = await workflow.execute_activity_method(
                    Activities.read_existing_apps, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
                )
                actions = plan(snapshot.apps, existing)
                workflow.logger.info("requests at %s: %d app step(s)", snapshot.commit, len(actions))
                await asyncio.gather(*(self._start_app(a) for a in actions))
            if not self._poked:
                return actions

    async def _start_app(self, action: AppAction) -> None:
        try:
            await workflow.execute_child_workflow(AppWorkflow.run, action, id=f"app-{action.app}")
        except WorkflowAlreadyStartedError:
            workflow.logger.info("app %s already has a running workflow - left to it", action.app)
