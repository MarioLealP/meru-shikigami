"""Temporal workflows (D65, D75). Workflow code is replayed from history after a restart, so it must stay
deterministic: no I/O, clock or randomness here - that all lives in the activities."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from shikigami.activities import Activities, BuildActivities
    from shikigami.models import AppAction
    from shikigami.plan import plan

SYNC_ID = "requests-sync"  # one id: pings while a run is open only queue one more pass (D65)
TIMEOUT = timedelta(minutes=2)  # per attempt
RETRY = RetryPolicy(maximum_attempts=5)  # then the run fails and the next ping or night tries again


def fingerprint(public_key: str) -> str:
    """A short stand-in for a public key in logs (the key itself is public, just long)."""
    body = "".join(line for line in public_key.splitlines() if "-----" not in line)
    return f"...{body[-12:]}" if body else "(none)"


@workflow.defn
class AppWorkflow:
    """One app's pass, as a child of requests-sync, so many apps run side by side and fail apart (D75). Requested apps
    walk their steps in order, each keeping what exists (D78): repo, build credentials (OnboardBuild, its own child),
    then the deploy files (D81). Retiring stays a dry run (only Anubis retires, D53)."""

    @workflow.run
    async def run(self, action: AppAction) -> None:
        if action.kind != "retire" and action.template and action.description:
            created = await workflow.execute_activity_method(
                Activities.ensure_repo,
                args=[action.app, action.template, action.description],
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
            )
            workflow.logger.info("app %s: repo %s", action.app, "created" if created else "kept")
            public_key = await workflow.execute_child_workflow(
                OnboardBuild.run, action.app, id=f"onboard-build-{action.app}"
            )
            workflow.logger.info("app %s: build credentials ready, signing key %s", action.app, fingerprint(public_key))
            if action.owner:
                deploy = await workflow.execute_activity_method(
                    Activities.ensure_deploy_files,
                    args=[action.app, action.owner, public_key],
                    start_to_close_timeout=TIMEOUT,
                    retry_policy=RETRY,
                )
                workflow.logger.info("app %s: deploy files %s", action.app, deploy)
        await workflow.execute_activity_method(
            Activities.dry_run, action, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )


@workflow.defn
class OnboardBuild:
    """An app's build credentials: build keys, then its Zot login (it reads the keys' entry), then its runner token.
    Returns the cosign public key. Each step keeps what exists, so a re-run only fills gaps."""

    @workflow.run
    async def run(self, app: str) -> str:
        public_key = await workflow.execute_activity_method(
            BuildActivities.ensure_build_keys, app, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )
        await workflow.execute_activity_method(
            BuildActivities.ensure_zot_login, app, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )
        await workflow.execute_activity_method(
            BuildActivities.ensure_runner_registration, app, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )
        return public_key


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
                # Planned against what Thoth had; then declare the whole request picture before the apps' steps run.
                summary = await workflow.execute_activity_method(
                    Activities.declare_apps, snapshot, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
                )
                workflow.logger.info(
                    "requests at %s: %d app step(s); Thoth: %s", snapshot.commit, len(actions), summary
                )
                await asyncio.gather(*(self._start_app(a) for a in actions))
            if not self._poked:
                return actions

    async def _start_app(self, action: AppAction) -> None:
        try:
            await workflow.execute_child_workflow(AppWorkflow.run, action, id=f"app-{action.app}")
        except WorkflowAlreadyStartedError:
            workflow.logger.info("app %s already has a running workflow - left to it", action.app)
