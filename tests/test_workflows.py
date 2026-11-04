import uuid
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.request_format import App
from shikigami.workflows import AppWorkflow, FirstBuild, OnboardBuild, RequestsSync, fingerprint

ECHO = App.model_validate(
    {
        "apiVersion": "meru.clockmost.com/v1",
        "kind": "App",
        "metadata": {"name": "echo"},
        "spec": {"owner": "mario", "description": "Placeholder app"},
    }
)


def sync_activities(
    snapshot: RequestsSnapshot,
    existing: dict[str, ExistingApp],
    done: list[AppAction],
    log: list[str],
):
    @activity.defn(name="read_requests")
    async def read_requests() -> RequestsSnapshot:
        log.append("read_requests")
        return snapshot

    @activity.defn(name="read_existing_apps")
    async def read_existing_apps() -> dict[str, ExistingApp]:
        log.append("read_existing_apps")
        return existing

    @activity.defn(name="declare_apps")
    async def declare_apps(declared: RequestsSnapshot) -> dict:
        log.append(f"declare_apps {sorted(declared.apps)}")
        return {"created": len(declared.apps)}

    @activity.defn(name="ensure_repo")
    async def ensure_repo(app: str, template: str, description: str) -> bool:
        log.append(f"ensure_repo {app} {template}")
        return app not in existing

    @activity.defn(name="dry_run")
    async def dry_run(action: AppAction) -> None:
        done.append(action)

    return [read_requests, read_existing_apps, declare_apps, ensure_repo, dry_run]


def onboard_activities(log: list[str], deploy: str, first_build: str | None):
    @activity.defn(name="ensure_deploy_files")
    async def ensure_deploy_files(app: str, owner: str, public_key: str, secrets: list[str]) -> str:
        log.append(f"ensure_deploy_files {app} {owner}")
        return deploy

    @activity.defn(name="first_build")
    async def first_build_step(app: str) -> str:
        log.append(f"first_build {app}")
        if first_build is None:
            raise ApplicationError("first build in_progress (attempt 1)", non_retryable=True)
        return first_build

    @activity.defn(name="lock_out")
    async def lock_out(app: str) -> str:
        log.append(f"lock_out {app}")
        return "kept"

    @activity.defn(name="ensure_trust")
    async def ensure_trust(app: str, description: str, public_key: str) -> str:
        log.append(f"ensure_trust {app}")
        return "fence kept; key kept"

    @activity.defn(name="ensure_build_keys")
    async def ensure_build_keys(app: str) -> str:
        log.append(f"ensure_build_keys {app}")
        return "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIabcdefghijkl\n-----END PUBLIC KEY-----\n"

    @activity.defn(name="ensure_zot_login")
    async def ensure_zot_login(app: str) -> bool:
        log.append(f"ensure_zot_login {app}")
        return False

    @activity.defn(name="ensure_runner_registration")
    async def ensure_runner_registration(app: str) -> bool:
        log.append(f"ensure_runner_registration {app}")
        return False

    return [
        ensure_trust,
        ensure_deploy_files,
        first_build_step,
        lock_out,
        ensure_build_keys,
        ensure_zot_login,
        ensure_runner_registration,
    ]


def retire_activities(log: list[str], retire: dict[str, object] | None):
    def retire_step(name: str, result):
        @activity.defn(name=name)
        async def step(app: str):
            log.append(f"{name} {app}")
            return result

        return step

    answers: dict[str, object] = {
        "retire_stop": "written",
        "retire_wait_stopped": "stopped",
        "retire_park_marker": "parked",
        "retire_trust": "fence proposed; key proposed; push rule proposed",
        "retire_runner": 1,
        "revoke_zot_login": True,
        "retire_archive": True,
        **(retire or {}),
    }
    retire_steps = [retire_step(name, result) for name, result in answers.items()]

    @activity.defn(name="retire_mark")
    async def retire_mark(app: str, status: str) -> str:
        log.append(f"retire_mark {app} {status}")
        return status

    @activity.defn(name="retire_mark_namespaces")
    async def retire_mark_namespaces(app: str, removed_at: datetime | None) -> list[str]:
        log.append(f"retire_mark_namespaces {app} {removed_at.isoformat() if removed_at else None}")
        return [app]

    @activity.defn(name="restore_unmark_namespaces")
    async def restore_unmark_namespaces(app: str) -> list[str]:
        log.append(f"restore_unmark_namespaces {app}")
        return [app]

    return [*retire_steps, retire_mark, retire_mark_namespaces, restore_unmark_namespaces]


def activities(
    snapshot: RequestsSnapshot,
    existing: dict[str, ExistingApp],
    done: list[AppAction],
    log: list[str] | None = None,
    deploy: str = "kept",
    first_build: str | None = "passed (attempt 1)",
    retire: dict[str, object] | None = None,
):
    """`first_build`: what the first_build activity answers; None = it never settles (fails for good). `retire`:
    answers for retire steps, by activity name, over the defaults."""
    log = [] if log is None else log
    return [
        *retire_activities(log, retire),
        *sync_activities(snapshot, existing, done, log),
        *onboard_activities(log, deploy, first_build),
    ]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        yield env


async def run_sync(
    client: Client,
    snapshot: RequestsSnapshot,
    existing: dict[str, ExistingApp],
    log: list[str] | None = None,
    deploy: str = "kept",
    first_build: str | None = "passed (attempt 1)",
    retire: dict[str, object] | None = None,
):
    done: list[AppAction] = []
    queue = f"test-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RequestsSync, AppWorkflow, OnboardBuild, FirstBuild],
        activities=activities(snapshot, existing, done, log, deploy, first_build, retire),
    ):
        result = await client.execute_workflow(RequestsSync.run, id=f"sync-{uuid.uuid4()}", task_queue=queue)
        for app in snapshot.apps:  # the first-build watch outlives the sync (D90): let it finish too
            with suppress(RPCError):  # never started
                await client.get_workflow_handle(f"first-build-{app}").result()
    return result, done


async def test_each_app_step_runs_in_its_own_child(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    gone = ExistingApp(key="old", status="active", template="nuwa/python")
    result, done = await run_sync(env.client, snapshot, {"old": gone})
    assert [(a.app, a.kind) for a in result] == [("echo", "register"), ("old", "retire")]
    assert sorted(a.app for a in done) == ["echo", "old"]


async def test_requests_with_problems_change_nothing(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=["apps/x.yaml: broken"])
    result, done = await run_sync(env.client, snapshot, {})
    assert result == []
    assert done == []


async def test_declares_after_planning_against_what_thoth_had(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    log: list[str] = []
    result, _ = await run_sync(env.client, snapshot, {}, log)
    assert log[:3] == ["read_requests", "read_existing_apps", "declare_apps ['echo']"]
    assert [(a.app, a.kind) for a in result] == [("echo", "register")]


async def test_every_requested_app_ensures_its_repo_and_retiring_touches_nothing(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    known = ExistingApp(key="echo", status="active", template="nuwa/python")
    gone = ExistingApp(key="old", status="active", template="nuwa/python")
    log: list[str] = []
    result, done = await run_sync(env.client, snapshot, {"echo": known, "old": gone}, log)
    assert [(a.app, a.kind) for a in result] == [("echo", "check"), ("old", "retire")]
    assert [line for line in log if line.startswith("ensure_repo")] == ["ensure_repo echo nuwa/python"]
    assert sorted(a.app for a in done) == ["echo", "old"]


async def test_build_credentials_follow_the_repo_in_order(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    gone = ExistingApp(key="old", status="active", template="nuwa/python")
    log: list[str] = []
    await run_sync(env.client, snapshot, {"old": gone}, log)
    steps = [line for line in log if line.split()[0].startswith(("ensure_", "first_build", "lock_out"))]
    assert steps == [
        "ensure_repo echo nuwa/python",
        "ensure_build_keys echo",
        "ensure_zot_login echo",
        "ensure_runner_registration echo",
        "ensure_trust echo",
        "ensure_deploy_files echo mario",
        "first_build echo",
        "lock_out echo",
    ]  # nothing for "old": retiring builds nothing


@pytest.mark.parametrize(
    ("deploy", "locks_out"),
    [
        ("kept", True),
        ("proposed in Meru/echo", False),  # the marker PR waits for Xolotl (D99)
        ("waiting on Meru/echo#1", False),
        ("waiting: no fence Meru/meru projects/echo.yaml", False),
    ],
)
async def test_shikigami_locks_itself_out_only_once_the_marker_is_in(
    env: WorkflowEnvironment, deploy: str, locks_out: bool
):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    log: list[str] = []
    await run_sync(env.client, snapshot, {}, log, deploy)
    assert ("lock_out echo" in log) == locks_out


async def test_a_first_build_that_never_settles_still_ends_in_the_lock_out(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    log: list[str] = []
    await run_sync(env.client, snapshot, {}, log, "kept", first_build=None)
    assert log[-2:] == ["first_build echo", "lock_out echo"]  # a broken build doesn't keep Shikigami admin
    result = await env.client.get_workflow_handle("first-build-echo").result()
    assert result == "first build not settled within an hour; lock-out kept"


def test_fingerprint_shows_the_end_of_the_key():
    key = "-----BEGIN PUBLIC KEY-----\nAAAA\nBBBBCCCCDDDDEEEE\n-----END PUBLIC KEY-----\n"
    assert fingerprint(key) == "...CCCCDDDDEEEE"
    assert fingerprint("") == "(none)"


async def test_requests_with_problems_declare_nothing(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=["apps/x.yaml: broken"])
    log: list[str] = []
    await run_sync(env.client, snapshot, {}, log)
    assert log == ["read_requests"]


async def test_a_removed_request_retires_in_order_and_onboards_nothing(env: WorkflowEnvironment):
    removed_at = datetime(2026, 11, 3, 12, 31, 18, tzinfo=UTC)
    golem = ECHO.model_copy(
        update={
            "metadata": ECHO.metadata.model_copy(update={"name": "golem", "removed_at": removed_at}),
            "spec": ECHO.spec.model_copy(update={"removed": True}),
        }
    )
    snapshot = RequestsSnapshot(commit="c1", apps={"golem": golem}, databases={}, problems=[])
    log: list[str] = []
    _, done = await run_sync(env.client, snapshot, {"golem": ExistingApp(key="golem", status="active")}, log)
    assert [line for line in log if line.startswith(("ensure_", "retire_", "revoke_"))] == [
        "retire_mark golem decommissioning",  # Thoth hears it first (D85)
        "retire_stop golem",  # stop first ...
        "retire_wait_stopped golem",  # ... confirmed by Argo before the Application can go
        "retire_park_marker golem",
        "retire_trust golem",  # trust once nothing uses it
        "retire_runner golem",
        "revoke_zot_login golem",
        "retire_archive golem",  # nothing is written to the repo after this
        "retire_mark_namespaces golem 2026-11-03T12:31:18+00:00",  # the request's removedAt: Charon's key (D107)
        "retire_mark golem retained",  # every step done: stopped, revoked, data kept until Charon
    ]
    assert done == []  # no dry run: the pass is real


@pytest.mark.parametrize(
    ("retire", "last"),
    [
        ({"retire_stop": "proposed in Meru/golem"}, "retire_stop golem"),
        ({"retire_stop": "waiting on Meru/golem#3"}, "retire_stop golem"),
        ({"retire_park_marker": "proposed in Meru/golem"}, "retire_park_marker golem"),
    ],
)
async def test_a_retire_pr_ends_the_pass_until_xolotl_merges_it(env: WorkflowEnvironment, retire: dict, last: str):
    golem = ECHO.model_copy(
        update={
            "metadata": ECHO.metadata.model_copy(update={"name": "golem"}),
            "spec": ECHO.spec.model_copy(update={"removed": True}),
        }
    )
    snapshot = RequestsSnapshot(commit="c1", apps={"golem": golem}, databases={}, problems=[])
    log: list[str] = []
    await run_sync(env.client, snapshot, {"golem": ExistingApp(key="golem", status="active")}, log, retire=retire)
    assert [line for line in log if line.startswith("retire_")][-1] == last  # nothing after it this pass


async def test_a_retained_app_whose_request_is_back_is_marked_planned_then_onboarded(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=[])
    log: list[str] = []
    retained = ExistingApp(key="echo", status="retained", template="nuwa/python")
    result, done = await run_sync(env.client, snapshot, {"echo": retained}, log)
    assert [(a.app, a.kind) for a in result] == [("echo", "restore")]
    steps = [line for line in log if line.startswith(("ensure_", "retire_", "restore_", "lock_out"))]
    assert steps[:3] == [
        "retire_mark echo planned",  # Thoth first (D106)
        "restore_unmark_namespaces echo",  # then the removed-at mark off: Charon must not purge it
        "ensure_repo echo nuwa/python",
    ]
    assert "ensure_deploy_files echo mario" in steps
    assert not any(s.startswith("retire_stop") for s in steps)
    assert [a.kind for a in done] == ["restore"]  # the closing dry-run line, as for any onboarding pass
