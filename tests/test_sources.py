import io
import json
import tarfile

import httpx2
import pytest
import yaml

from shikigami.models import RequestsSnapshot
from shikigami.request_format import App
from shikigami.sources import (
    Proposal,
    check_proposal,
    declare_apps,
    mark_retire_step,
    read_existing_apps,
    read_requests,
)

ECHO = b"""\
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: echo
spec:
  owner: mario
  description: Placeholder app
"""


def archive(files: dict[str, bytes]) -> bytes:
    """A tarball the way Gitea serves one: everything under a <repo>/ folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"requests/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def gitea(files: dict[str, bytes], seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/branches/main"):
            return httpx2.Response(200, json={"commit": {"id": "abc123"}})
        if request.url.path.endswith("/archive/abc123.tar.gz"):
            return httpx2.Response(200, content=archive(files))
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_reads_the_commit_it_names():
    seen: list[httpx2.Request] = []
    async with gitea({"apps/echo.yaml": ECHO, "README.md": b"hi"}, seen) as http:
        snapshot = await read_requests(http, "https://git.test", "Meru/requests", "main", "tok")
    assert snapshot.commit == "abc123"
    assert list(snapshot.apps) == ["echo"]
    assert snapshot.problems == []
    assert all(r.headers["authorization"] == "token tok" for r in seen)


async def test_a_bad_file_is_a_problem_not_a_crash():
    async with gitea({"apps/echo.yaml": ECHO, "notes.txt": b"stray"}, []) as http:
        snapshot = await read_requests(http, "https://git.test", "Meru/requests", "main", "tok")
    assert snapshot.problems == [
        "notes.txt: not a request - only apps/<name>.yaml and databases/<name>.yaml belong here"
    ]


async def test_thoth_apps_by_key():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["type"] == "app"
        assert request.headers["authorization"] == "Bearer t"
        item = {"key": "echo", "status": "active", "attributes": {"template": "nuwa/python"}}
        return httpx2.Response(200, json=[item])

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        apps = await read_existing_apps(http, "http://thoth.test", "t")
    assert apps["echo"].template == "nuwa/python"


async def test_declares_every_requested_app_as_one_snapshot():
    app = App.model_validate(yaml.safe_load(ECHO))
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"created": 1})

    snapshot = RequestsSnapshot(commit="abc123", apps={"echo": app}, databases={}, problems=[])
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        summary = await declare_apps(http, "http://thoth.test", "t", snapshot)
    assert summary == {"created": 1}
    [request] = seen
    assert request.method == "PUT"
    assert request.url.path == "/v1/scans/requests-abc123/shikigami/all/items"
    assert request.headers["authorization"] == "Bearer t"
    assert json.loads(request.content) == {
        "items": [{"type": "app", "key": "echo", "name": "echo", "attributes": {"template": "nuwa/python"}}]
    }


def app_file(name: str, host: str | None = None) -> str:
    route = f"  route:\n    host: {host}\n" if host else ""
    return (
        f"apiVersion: meru.clockmost.com/v1\nkind: App\nmetadata:\n  name: {name}\n"
        f"spec:\n  owner: mario\n  description: A test app\n{route}"
    )


async def check(files: dict[str, bytes], path: str, content: str):
    async with gitea(files, []) as http:
        return await check_proposal(
            http, "https://git.test", "Meru/requests", "main", "tok", Proposal(path=path, content=content)
        )


async def test_a_good_proposal_has_no_problems_and_names_the_main_it_was_checked_on():
    verdict = await check({"apps/echo.yaml": ECHO}, "apps/golem.yaml", app_file("golem"))
    assert verdict.commit == "abc123"
    assert verdict.problems == []


async def test_a_bad_proposal_gets_its_problems_with_paths_relative_to_the_repo():
    verdict = await check({"apps/echo.yaml": ECHO}, "apps/golem.yaml", app_file("other"))
    assert verdict.problems == ["apps/golem.yaml: metadata.name 'other' must match the file name 'golem'"]


async def test_checks_across_files_count():
    verdict = await check({"apps/echo.yaml": ECHO}, "apps/golem.yaml", app_file("golem", host="echo.clockmost.com"))
    assert verdict.problems == ["route.host echo.clockmost.com is used by 2 apps"]


async def test_problems_main_already_has_are_not_blamed_on_the_proposal():
    verdict = await check({"apps/echo.yaml": ECHO, "notes.txt": b"stray"}, "apps/golem.yaml", app_file("golem"))
    assert verdict.problems == []


async def test_not_yaml_is_a_problem_not_a_crash():
    verdict = await check({"apps/echo.yaml": ECHO}, "apps/golem.yaml", "spec: [unclosed")
    assert len(verdict.problems) == 1
    assert verdict.problems[0].startswith("apps/golem.yaml: not YAML")


async def test_retire_steps_are_told_to_thoth():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
        return httpx2.Response(200, json={"status": "decommissioning"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        assert await mark_retire_step(http, "http://thoth", "tok", "golem", "decommissioning") == "decommissioning"
    assert seen == [
        (
            "/v1/retire-steps",
            "Bearer tok",
            {"app": "golem", "status": "decommissioning", "reason": "Shikigami's retire pass (D85)"},
        )
    ]


@pytest.mark.parametrize("status", [403, 404, 409, 422])  # 422: a Thoth from before restores (D106)
async def test_a_refused_retire_step_is_said_and_never_stops_the_pass(status: int):
    async def call(code: int) -> str:
        transport = httpx2.MockTransport(lambda request: httpx2.Response(code, json={"detail": "no"}))
        async with httpx2.AsyncClient(transport=transport) as http:
            return await mark_retire_step(http, "http://thoth", "tok", "golem", "retained")

    assert (await call(status)).startswith(f"not marked retained: Thoth answered {status}")
    with pytest.raises(httpx2.HTTPStatusError):  # a server error is retried by the activity
        await call(503)
