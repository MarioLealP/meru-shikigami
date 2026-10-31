import json

import httpx2
import pytest

from shikigami.repos import NoTemplate, ensure_repo, template_repo


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
