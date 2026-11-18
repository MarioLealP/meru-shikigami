import io
import json
import tarfile
from pathlib import Path

import httpx2
import pytest
import yaml

from shikigami.clients.sources import (
    Proposal,
    check_proposal,
    declare_apps,
    mark_retire_step,
    read_existing_apps,
    read_requests,
)
from shikigami.models.request_format import App
from shikigami.models.sync import ExistingApp, RequestsSnapshot

K8S_TOKEN = Path(__file__).parent.parent / "fixtures" / "k8s-token"  # what the kubelet mounts

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


STOPPED_VALUES = "owner: mario\nenabled: false # retired (D85): no workload until Charon purges the namespace\n"
MARKER = "owner: mario\nbuild: true\n"


async def existing_apps(
    files: dict[str, dict[str, str]], deployed: set[str], names: list[str]
) -> tuple[dict[str, ExistingApp], list[httpx2.Request]]:
    """read_existing_apps over fake Gitea (repos + raw files) and fake Argo CD (Applications); both record calls.

    `files` is name -> path -> text for the app's repo (no entry = no repo); `deployed` names the apps whose
    `app-<name>` Application exists. The fakes answer the real URLs but only what the real ones would say for
    these states - see the PR for what this does not cover.
    """
    seen: list[httpx2.Request] = []

    def gitea(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        _, _, _, _, _, name, *rest = request.url.path.split("/")
        if not rest:  # /api/v1/repos/<org>/<name>: repo exists?
            return httpx2.Response(200, json={}) if name in files else httpx2.Response(404)
        text = files.get(name, {}).get("/".join(rest[1:]))  # /raw/<path>
        return httpx2.Response(200, text=text) if text is not None else httpx2.Response(404)

    def kube(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        app = request.url.path.rsplit("/", 1)[-1].removeprefix("app-")  # .../namespaces/argocd/applications/app-<name>
        assert request.url.path == f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/app-{app}"
        return httpx2.Response(200, json={"status": {}}) if app in deployed else httpx2.Response(404)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(gitea)) as http,
        httpx2.AsyncClient(transport=httpx2.MockTransport(kube)) as k8s,
    ):
        apps = await read_existing_apps(
            http, k8s, "https://git.test", "Meru", "https://k8s.test", K8S_TOKEN, "argocd", names
        )
    return apps, seen


async def test_what_exists_is_read_from_gitea_and_argo_and_never_thoth():
    files = {
        "echo": {"deploy/values.yaml": "owner: mario\n", "deploy/meru.yaml": MARKER},
        "golem": {"deploy/values.yaml": STOPPED_VALUES},  # stopped, marker parked: retained
        "resto": {"deploy/values.yaml": STOPPED_VALUES, "deploy/meru.yaml": MARKER},  # stopped, marker up
        "by-hand": {"deploy/meru.yaml": MARKER},  # a repo without deploy/values.yaml
    }
    names = ["echo", "golem", "resto", "by-hand", "ghost", "new"]
    apps, seen = await existing_apps(files, {"echo", "ghost"}, names)
    assert {name: app.status for name, app in apps.items()} == {
        "echo": "active",  # repo + Application, nothing stopped
        "golem": "retained",  # its retire pass parked the marker (D85)
        "resto": "decommissioning",  # stopped but the marker is still up
        "by-hand": "active",  # a repo counts even with no Application
        "ghost": "active",  # so does an Application with no readable repo
    }
    assert all(app.template is None for app in apps.values())  # neither source records the template
    assert "new" not in apps  # neither repo nor Application: plan() registers it from scratch
    assert all(r.url.host in {"git.test", "k8s.test"} for r in seen)  # the plan path never reaches Thoth
    asked = [r.url.path.rsplit("/", 1)[-1] for r in seen if "applications" in r.url.path]
    assert sorted(asked) == sorted(f"app-{name}" for name in names)  # one get per name, no list (the rbac allows get)


async def test_a_repo_only_app_counts_as_active():
    apps, seen = await existing_apps({"mid": {"deploy/values.yaml": "owner: mario\n"}}, set(), ["mid"])
    assert {name: app.status for name, app in apps.items()} == {"mid": "active"}
    assert [r.url.path for r in seen if "/raw/" in r.url.path] == ["/api/v1/repos/Meru/mid/raw/deploy/values.yaml"]


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
    assert verdict.problems == ["route.host echo.clockmost.com is used by 2 apps or environments"]


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
