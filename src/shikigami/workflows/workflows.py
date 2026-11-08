"""Temporal workflows (D65, D75).

Workflow code is replayed from history after a restart, so it must stay deterministic: no I/O, clock or randomness here
- that all lives in the activities.
"""

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from shikigami.activities import Activities, BuildActivities
    from shikigami.core.plan import plan
    from shikigami.models.sync import AppAction

SYNC_ID = "requests-sync"  # one id: pings while a run is open only queue one more pass (D65)
TIMEOUT = timedelta(minutes=2)  # per attempt
RETRY = RetryPolicy(maximum_attempts=5)  # then the run fails and the next ping or night tries again
# Argo applies a commit within ~3 min (poll 120 s + 60 s jitter); look every 30 s for 15 min, then the run fails
WAIT_STOPPED = RetryPolicy(initial_interval=timedelta(seconds=30), backoff_coefficient=1.0, maximum_attempts=30)
# A first build can wait up to 40 min for memory (scripts/build.sh) and then takes a few: look every minute for an hour
FIRST_BUILD_POLL = RetryPolicy(initial_interval=timedelta(minutes=1), backoff_coefficient=1.0, maximum_attempts=60)


def fingerprint(public_key: str) -> str:
    """A short stand-in for a public key in logs (the key itself is public, just long)."""
    body = "".join(line for line in public_key.splitlines() if "-----" not in line)
    return f"...{body[-12:]}" if body else "(none)"


@workflow.defn
class AppWorkflow:
    """One app's pass, as a child of requests-sync, so many apps run side by side and fail apart (D75).

    Requested apps walk their steps in order, each keeping what exists (D78): repo, build credentials (OnboardBuild, its
    own child), then its fence + signing key, proposed as PRs (D82), then the deploy files (D81), then Shikigami locks
    itself out of the repo (D89). A request marked removed runs the retire pass (D85); an app with no request at all
    stays a dry run.
    """

    @workflow.run
    async def run(self, action: AppAction) -> None:
        """Run one app's steps; a retire pass replaces them, a restore goes first."""
        if action.kind == "restore":  # D106: Thoth first, then onboarding again - every step keeps what exists
            said = await workflow.execute_activity_method(
                Activities.retire_mark, args=[action.app, "planned"], start_to_close_timeout=TIMEOUT, retry_policy=RETRY
            )
            workflow.logger.info("app %s: restore - Thoth %s", action.app, said)
            unmark_args: list = [action.app]
            if workflow.patched("retire-environments"):  # D126: the environments' namespaces too
                unmark_args.append(action.environments)
            unmarked = await workflow.execute_activity_method(
                Activities.restore_unmark_namespaces,
                args=unmark_args,
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
            )
            workflow.logger.info("app %s: restore - removed-at off %s", action.app, unmarked or "(no namespace)")
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
            trust_args: list = [action.app, action.description, public_key]
            # D126, behind a patch: a pass already running on the old code replays with the old arguments.
            if workflow.patched("environments"):
                trust_args.append(action.environments)
            trust = await workflow.execute_activity_method(
                Activities.ensure_trust,
                args=trust_args,
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRY,
            )
            workflow.logger.info("app %s: %s", action.app, trust)
            if action.owner:
                deploy_args = [action.app, action.owner, public_key, action.secrets, action.secret_entries]
                # D125, behind a patch: a pass already running on the old code replays without this step.
                if workflow.patched("glitchtip-dsn"):
                    dsn = await workflow.execute_activity_method(
                        Activities.ensure_glitchtip, action.app, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
                    )
                    deploy_args.append(dsn)
                deploy = await workflow.execute_activity_method(
                    Activities.ensure_deploy_files,
                    args=deploy_args,
                    start_to_close_timeout=TIMEOUT,
                    retry_policy=RETRY,
                )
                workflow.logger.info("app %s: deploy files %s", action.app, deploy)
                if deploy == "kept":  # marker in (Xolotl merged it, D99): the repo is the app's now (D89)
                    await self._first_build_then_lock_out(action.app)
        if action.kind == "retire" and action.template:  # removed by its request (D83); no request at all: dry run
            await self._retire(action.app, action.removed_at, action.environments)
            return
        await workflow.execute_activity_method(
            Activities.dry_run, action, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )

    async def _first_build_then_lock_out(self, app: str) -> None:
        """Its own workflow, not awaited (D90): the wait can take an hour, and requests-sync waits for every app.

        One id per app, so a later pass that finds it still watching leaves it be - and doesn't lock out early.
        """
        try:
            await workflow.start_child_workflow(
                FirstBuild.run, app, id=f"first-build-{app}", parent_close_policy=ParentClosePolicy.ABANDON
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info("app %s: first build still watched - lock-out follows it", app)

    async def _retire(self, app: str, removed_at: datetime | None, environments: list[str]) -> None:
        """D85, in order.

        The stop must be applied before the Application goes - preserveResourcesOnDeletion would leave running pods
        behind - and the trust goes once nothing uses it. The repo is archived last. Thoth hears decommissioning at the
        start and retained once every step is done (only Anubis retires).
        """
        # D126, behind a patch: a pass already running on the old code replays with the old arguments.
        envs: list = [environments] if workflow.patched("retire-environments") else []

        async def step(method, retry_policy: RetryPolicy = RETRY, with_envs: bool = False):  # an activity method
            return await workflow.execute_activity_method(
                method,
                args=[app, *(envs if with_envs else [])],
                start_to_close_timeout=TIMEOUT,
                retry_policy=retry_policy,
            )

        async def mark(status: str) -> None:
            said = await workflow.execute_activity_method(
                Activities.retire_mark, args=[app, status], start_to_close_timeout=TIMEOUT, retry_policy=RETRY
            )
            workflow.logger.info("app %s: retire - Thoth %s", app, said)

        await mark("decommissioning")
        stop = await step(Activities.retire_stop, with_envs=True)
        workflow.logger.info("app %s: retire - stop %s", app, stop)
        if not (stop.startswith("kept") or stop == "written"):  # a PR (cm-konk): Xolotl's merge pings the next pass
            return
        workflow.logger.info(
            "app %s: retire - Argo %s", app, await step(Activities.retire_wait_stopped, WAIT_STOPPED, with_envs=True)
        )
        marker = await step(Activities.retire_park_marker)
        workflow.logger.info("app %s: retire - marker %s", app, marker)
        if marker not in ("kept", "parked"):  # a PR, as above
            return
        workflow.logger.info("app %s: retire - %s", app, await step(Activities.retire_trust, with_envs=True))
        workflow.logger.info("app %s: retire - %d runner(s) removed", app, await step(Activities.retire_runner))
        zot = await step(BuildActivities.revoke_zot_login)
        workflow.logger.info("app %s: retire - Zot login %s", app, "revoked" if zot else "already gone")
        archived = await step(Activities.retire_archive)
        workflow.logger.info("app %s: retire - repo %s", app, "archived" if archived else "not archived here")
        marked = await workflow.execute_activity_method(
            Activities.retire_mark_namespaces,
            args=[app, removed_at, *envs],
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
        )
        workflow.logger.info("app %s: retire - removed-at on %s", app, marked or "(no namespace)")
        await mark("retained")


@workflow.defn
class OnboardBuild:
    """An app's build credentials: build keys, then its Zot login (it reads the keys' entry), then its runner token.

    Returns the cosign public key. Each step keeps what exists, so a re-run only fills gaps.
    """

    @workflow.run
    async def run(self, app: str) -> str:
        """Make whatever is missing; returns the cosign public key."""
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
class FirstBuild:
    """D90: wait for the app's first build, then lock out (D89) whatever came of it.

    A failed build is re-run up to first_build_reruns times; a build that never passes doesn't keep Shikigami admin. One
    hour at most.
    """

    @workflow.run
    async def run(self, app: str) -> str:
        """Watch the first build, lock out, and say how both went."""
        try:
            build = await workflow.execute_activity_method(
                Activities.first_build, app, start_to_close_timeout=TIMEOUT, retry_policy=FIRST_BUILD_POLL
            )
        except ActivityError:
            build = "not settled within an hour"
        workflow.logger.info("app %s: first build %s", app, build)
        locked = await workflow.execute_activity_method(
            Activities.lock_out, app, start_to_close_timeout=TIMEOUT, retry_policy=RETRY
        )
        workflow.logger.info("app %s: lock-out %s", app, locked)
        return f"first build {build}; lock-out {locked}"


@workflow.defn
class RequestsSync:
    """Reads the requests and what exists, works out the difference and hands each app to its own child workflow.

    A `poke` while running means "look again": the run makes one more pass before it ends.
    """

    def __init__(self) -> None:
        self._poked = False

    @workflow.signal
    def poke(self) -> None:
        """Ask for one more pass after this one."""
        self._poked = True

    @workflow.run
    async def run(self) -> list[AppAction]:
        """Sync until no poke arrived during a pass; returns the last pass's actions."""
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
