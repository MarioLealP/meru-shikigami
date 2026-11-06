"""D125: the app's GlitchTip project and DSN, and the DSN in deploy/values.yaml."""

import json
import logging

import httpx2
import pytest
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment

from shikigami.activities import Activities
from shikigami.activities.deploy import _deploy_files
from shikigami.clients.glitchtip import project_dsn
from shikigami.config import Settings
from shikigami.core.marker import with_glitchtip

DSN = "https://0123456789abcdef0123456789abcdef@glitchtip.clockmost.com/7"
OTHER = "https://fedcba9876543210fedcba9876543210@glitchtip.clockmost.com/7"
API = "https://glitchtip.test/api/0"


class GlitchTip:
    """The few GlitchTip calls Shikigami makes, against one org with one team."""

    def __init__(self, projects: set[str], keys: list[dict] | None = None, slug: str | None = None) -> None:
        self.projects = projects
        self.keys = keys if keys is not None else [{"isActive": True, "dsn": {"public": DSN, "secret": "x"}}]
        self.slug = slug  # what a create answers; None: the name asked for
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == "Bearer gt-token"
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if request.method == "GET" and path.endswith("/keys/"):
            return httpx2.Response(200, json=self.keys)
        if request.method == "POST" and path.endswith("/keys/"):
            return httpx2.Response(201, json={"isActive": True, "dsn": {"public": OTHER}})
        if request.method == "GET" and path.startswith("/api/0/projects/meru/"):
            app = path.removeprefix("/api/0/projects/meru/").strip("/")
            return httpx2.Response(200 if app in self.projects else 404, json={"slug": app})
        if request.method == "POST" and path == "/api/0/teams/meru/meru/projects/":
            assert body is not None
            self.projects.add(body["name"])
            return httpx2.Response(201, json={"slug": self.slug or body["name"]})
        raise AssertionError(f"{request.method} {path}")

    async def dsn(self, app: str = "echo") -> tuple[str, bool]:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return await project_dsn(http, "https://glitchtip.test", SecretStr("gt-token"), "meru", "meru", app)


async def test_an_existing_project_gives_its_dsn():
    gt = GlitchTip({"echo"})
    assert await gt.dsn() == (DSN, False)
    assert [c[0] for c in gt.calls] == ["GET", "GET"]  # read only


async def test_a_missing_project_is_made_under_the_meru_team():
    gt = GlitchTip(set())
    assert await gt.dsn() == (DSN, True)
    assert ("POST", "/api/0/teams/meru/meru/projects/", {"name": "echo", "platform": "python"}) in gt.calls


async def test_a_suffixed_slug_is_refused_rather_than_wired():
    gt = GlitchTip(set(), slug="echo-1")
    with pytest.raises(ValueError, match="echo-1"):
        await gt.dsn()


async def test_a_key_answer_without_is_active_counts_as_active():
    # The shape GlitchTip 6.2.6 really answers (heimdall's first project, 2026-11-06): no isActive.
    gt = GlitchTip({"echo"}, keys=[{"name": "", "rateLimit": None, "dsn": {"public": DSN}, "projectID": 1}])
    assert await gt.dsn() == (DSN, False)


async def test_no_active_key_gets_one_made():
    gt = GlitchTip({"echo"}, keys=[{"isActive": False, "dsn": {"public": DSN}}])
    assert await gt.dsn() == (OTHER, False)


def activities(gt: GlitchTip, token: str | None) -> Activities:
    settings = Settings.model_construct(
        glitchtip_url="https://glitchtip.test",
        glitchtip_org="meru",
        glitchtip_team="meru",
        glitchtip_token=SecretStr(token) if token else None,
    )
    return Activities.model_construct(settings=settings, http=httpx2.AsyncClient(transport=httpx2.MockTransport(gt)))


async def test_without_a_token_no_dsn_and_no_call():
    gt = GlitchTip(set())
    assert await ActivityEnvironment().run(activities(gt, None).ensure_glitchtip, "echo") is None
    assert gt.calls == []


async def test_a_made_project_logs_its_dsn(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    gt = GlitchTip(set())
    assert await ActivityEnvironment().run(activities(gt, "gt-token").ensure_glitchtip, "echo") == DSN
    assert f"app echo: GlitchTip project created, DSN {DSN}" in caplog.text


# The contract with Xolotl, byte for byte: its tests/test_marker_pr.py expects this same text.
BLOCK = (
    "\n# Crash reports (D125): this app's GlitchTip project, which Shikigami made at onboarding.\n"
    f"glitchtip:\n  dsn: {DSN}\n"
)


def test_the_dsn_is_appended():
    assert with_glitchtip("name: echo\n", DSN) == "name: echo\n" + BLOCK
    assert with_glitchtip("name: echo", DSN) == "name: echo\n" + BLOCK


def test_no_dsn_or_a_set_glitchtip_keeps_the_file():
    assert with_glitchtip("name: echo\n", None) == "name: echo\n"
    own = "name: echo\nglitchtip:\n  dsn: ''\n"
    assert with_glitchtip(own, DSN) == own


def test_deploy_files_put_the_dsn_between_owner_and_secrets():
    files = _deploy_files('owner: ""\n', 'name: echo\nowner: ""\n', "mario", ["a-token"], None, DSN)
    after = files["deploy/values.yaml"].after
    assert after is not None
    assert after.startswith("name: echo\nowner: mario\n" + BLOCK + "\n# Secrets from the request")
