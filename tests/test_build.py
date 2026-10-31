import json
import uuid

import bcrypt
import httpx2
import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from shikigami import build_creds as creds
from shikigami.activities import BuildActivities
from shikigami.openbao import KV, Conflict
from shikigami.workflows import OnboardBuild

REGISTRY = "zot.clockmost.com"
OTHER = "echo-builder:" + bcrypt.hashpw(b"echo-pw", bcrypt.gensalt(4)).decode()


def openbao(store: dict[str, list[dict[str, str] | None]]) -> httpx2.AsyncClient:
    """A KV v2 mount answering the way OpenBao 2.7 does (checked against a dev server): 404 `{"errors":[]}` for no
    entry, 404 with metadata for a deleted latest version, 400 "check-and-set ..." for a stale cas. Each path keeps
    its versions; None is a deleted one."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/v1/apps/data/")
        versions = store.setdefault(path, [])
        if request.method == "GET":
            if not versions:
                return httpx2.Response(404, json={"errors": []})
            metadata = {"version": len(versions)}
            if versions[-1] is None:
                return httpx2.Response(404, json={"data": {"data": None, "metadata": metadata}})
            return httpx2.Response(200, json={"data": {"data": versions[-1], "metadata": metadata}})
        body = json.loads(request.content)
        if body["options"]["cas"] != len(versions):
            return httpx2.Response(400, json={"errors": ["check-and-set parameter did not match the current version"]})
        versions.append(body["data"])
        return httpx2.Response(200, json={"data": {"version": len(versions)}})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


def kv(store: dict[str, list[dict[str, str] | None]]) -> KV:
    async def token() -> str:
        return "t"

    return KV(openbao(store), "https://openbao.test", "apps", token)


async def fake_new_key(cosign: str) -> tuple[str, str, str]:
    return "PRIVATE", "key-pw", "PUBLIC"


async def fake_public_key(cosign: str, key: str, password: str) -> str:
    assert (key, password) == ("PRIVATE", "key-pw")
    return "PUBLIC"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        yield env


@pytest.fixture
def no_cosign(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(creds, "new_cosign_key", fake_new_key)
    monkeypatch.setattr(creds, "cosign_public_key", fake_public_key)


def build_activities(store: dict, runner: str = "runner-token") -> BuildActivities:
    async def runner_token(app: str) -> str:
        return runner

    return BuildActivities(kv(store), REGISTRY, "cosign", runner_token)


def test_htpasswd_line_added_kept_and_replaced():
    added = creds.htpasswd_with(OTHER + "\n", "demo-builder", "pw")
    assert added is not None
    assert added.splitlines()[0] == OTHER  # other apps' lines stay exactly as they were
    assert creds.htpasswd_with(added, "demo-builder", "pw") is None  # already right: nothing to write
    replaced = creds.htpasswd_with(added, "demo-builder", "new-pw")
    assert replaced is not None
    assert len(replaced.splitlines()) == 2
    assert creds.htpasswd_with(replaced, "demo-builder", "new-pw") is None


def test_zot_config_round_trip():
    config = creds.zot_config(REGISTRY, "demo-builder", "p:w")
    assert creds.zot_login(config, REGISTRY) == ("demo-builder", "p:w")


async def test_kv_reads_missing_deleted_and_conflicts():
    store: dict[str, list[dict[str, str] | None]] = {"gone": [{"a": "1"}, None]}
    bao = kv(store)
    assert (await bao.read("none")).version == 0
    deleted = await bao.read("gone")
    assert (deleted.data, deleted.version) == (None, 2)
    with pytest.raises(Conflict):
        await bao.write("gone", {"a": "2"}, cas=0)
    await bao.write("gone", {"a": "2"}, cas=2)
    assert (await bao.read("gone")).data == {"a": "2"}


@pytest.mark.usefixtures("no_cosign")
async def test_onboarding_makes_once_then_keeps():
    store: dict = {"meru/zot/eso/zot-htpasswd": [{"htpasswd": OTHER + "\n"}]}
    acts, env = build_activities(store), ActivityEnvironment()
    for first in (True, False):
        assert await env.run(acts.ensure_build_keys, "demo") == "PUBLIC"
        assert await env.run(acts.ensure_zot_login, "demo") is first
        assert await env.run(acts.ensure_runner_registration, "demo") is first
    assert len(store["meru/demo-build/build"]) == 1  # written once
    assert store["meru/demo-build/eso/sindri-registration"] == [{"token": "runner-token"}]
    user, password = creds.zot_login(creds.unb64(store["meru/demo-build/build"][0]["zot_config_b64"]), REGISTRY)
    htpasswd = store["meru/zot/eso/zot-htpasswd"][-1]["htpasswd"]
    assert htpasswd.splitlines()[0] == OTHER
    assert creds.htpasswd_with(htpasswd, user, password) is None


@pytest.mark.usefixtures("no_cosign")
async def test_half_a_build_entry_is_left_for_a_human():
    store: dict = {"meru/demo-build/build": [{"zot_config_b64": "x"}]}
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(build_activities(store).ensure_build_keys, "demo")
    assert error.value.non_retryable


async def test_onboard_build_runs_the_steps_in_order(env: WorkflowEnvironment):
    done: list[str] = []

    def step(name: str, result):
        @activity.defn(name=name)
        async def run(app: str):
            done.append(f"{name}:{app}")
            return result

        return run

    steps = [
        step("ensure_build_keys", "PUBLIC"),
        step("ensure_zot_login", True),
        step("ensure_runner_registration", True),
    ]
    queue = f"test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=[OnboardBuild], activities=steps):
        result = await env.client.execute_workflow(
            OnboardBuild.run, "demo", id=f"onboard-{uuid.uuid4()}", task_queue=queue
        )
    assert result == "PUBLIC"
    assert done == ["ensure_build_keys:demo", "ensure_zot_login:demo", "ensure_runner_registration:demo"]
