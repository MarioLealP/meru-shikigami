import uuid

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.request_format import App
from shikigami.workflows import AppWorkflow, RequestsSync

ECHO = App.model_validate(
    {
        "apiVersion": "meru.clockmost.com/v1",
        "kind": "App",
        "metadata": {"name": "echo"},
        "spec": {"owner": "mario", "description": "Placeholder app"},
    }
)


def activities(
    snapshot: RequestsSnapshot, existing: dict[str, ExistingApp], done: list[AppAction], log: list[str] | None = None
):
    log = [] if log is None else log

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

    @activity.defn(name="dry_run")
    async def dry_run(action: AppAction) -> None:
        done.append(action)

    return [read_requests, read_existing_apps, declare_apps, dry_run]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        yield env


async def run_sync(
    client: Client, snapshot: RequestsSnapshot, existing: dict[str, ExistingApp], log: list[str] | None = None
):
    done: list[AppAction] = []
    queue = f"test-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RequestsSync, AppWorkflow],
        activities=activities(snapshot, existing, done, log),
    ):
        result = await client.execute_workflow(RequestsSync.run, id=f"sync-{uuid.uuid4()}", task_queue=queue)
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
    assert log == ["read_requests", "read_existing_apps", "declare_apps ['echo']"]
    assert [(a.app, a.kind) for a in result] == [("echo", "register")]


async def test_requests_with_problems_declare_nothing(env: WorkflowEnvironment):
    snapshot = RequestsSnapshot(commit="c1", apps={"echo": ECHO}, databases={}, problems=["apps/x.yaml: broken"])
    log: list[str] = []
    await run_sync(env.client, snapshot, {}, log)
    assert log == ["read_requests"]
