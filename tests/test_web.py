from typing import cast

import httpx2
import pytest
from fastapi.testclient import TestClient
from temporalio.client import Client
from test_sources import ECHO, app_file, gitea

from shikigami.config import Settings
from shikigami.web import make_app


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHIKIGAMI_GITEA_TOKEN", "tok")
    http = gitea({"apps/echo.yaml": ECHO}, [])
    return TestClient(make_app(Settings(), cast(Client, None), http))  # /validate never touches Temporal


def test_validate_answers_with_the_problems(web: TestClient):
    r = web.post("/validate", json={"path": "apps/golem.yaml", "content": app_file("other")})
    assert r.status_code == 200
    assert r.json() == {
        "commit": "abc123",
        "problems": ["apps/golem.yaml: metadata.name 'other' must match the file name 'golem'"],
    }


@pytest.mark.parametrize(
    "path", ["../etc/passwd", "apps/../x.yaml", "apps/x.yml", "README.md", "apps/sub/x.yaml", "apps/X.yaml"]
)
def test_validate_takes_only_request_paths(web: TestClient, path: str):
    assert web.post("/validate", json={"path": path, "content": ""}).status_code == 422


def test_validate_refuses_oversized_content(web: TestClient):
    r = web.post("/validate", json={"path": "apps/golem.yaml", "content": "x" * 70000})
    assert r.status_code == 422


def test_validate_says_502_when_gitea_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHIKIGAMI_GITEA_TOKEN", "tok")
    down = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(500)))
    web = TestClient(make_app(Settings(), cast(Client, None), down))
    assert web.post("/validate", json={"path": "apps/golem.yaml", "content": "x"}).status_code == 502
