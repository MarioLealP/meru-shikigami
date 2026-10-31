import json

import httpx2
import pytest

from shikigami.repos import NoTemplate, ensure_repo, runner_token, template_repo


def gitea(status: int, seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json={})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_nuwa_templates_map_to_template_repos():
    assert template_repo("nuwa/python") == "nuwa-python"
    for other in ("https://git.example/x.git", "nuwa/", "python"):
        with pytest.raises(NoTemplate):
            template_repo(other)


async def test_generates_the_repo_from_its_template():
    seen: list[httpx2.Request] = []
    async with gitea(201, seen) as http:
        assert await ensure_repo(http, "https://git.test", "tok", "Meru", "echo", "nuwa/python", "Placeholder app")
    [request] = seen
    assert request.url.path == "/api/v1/repos/Meru/nuwa-python/generate"
    assert request.headers["authorization"] == "token tok"
    assert json.loads(request.content) == {
        "owner": "Meru",
        "name": "echo",
        "description": "Placeholder app",
        "private": False,
        "git_content": True,
    }


async def test_an_existing_repo_is_kept():
    async with gitea(409, []) as http:
        assert not await ensure_repo(http, "https://git.test", "tok", "Meru", "echo", "nuwa/python", "x")


async def test_a_missing_template_repo_is_named():
    with pytest.raises(NoTemplate, match="no template repo Meru/nuwa-upstream"):
        async with gitea(404, []) as http:
            await ensure_repo(http, "https://git.test", "tok", "Meru", "echo", "nuwa/upstream", "x")


async def test_runner_token_comes_from_the_repo():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"token": "reg-token"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        assert await runner_token(http, "https://git.test", "tok", "Meru", "echo") == "reg-token"
    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/api/v1/repos/Meru/echo/actions/runners/registration-token")
    assert request.headers["authorization"] == "token tok"


async def test_runner_token_refusal_is_raised():
    seen: list[httpx2.Request] = []
    async with gitea(403, seen) as http:
        with pytest.raises(httpx2.HTTPStatusError):
            await runner_token(http, "https://git.test", "tok", "Meru", "echo")
