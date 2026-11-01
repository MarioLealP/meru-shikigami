import json

import httpx2
import pytest

from shikigami.repos import (
    NoTemplate,
    add_writer,
    become_reader,
    ensure_repo,
    exists,
    is_admin,
    protect_main,
    runner_token,
    template_repo,
)


def gitea(status: int, seen: list[httpx2.Request], repo_status: int = 404) -> httpx2.AsyncClient:
    """Every call answers `status`, except the anonymous "does the repo exist?" read (`repo_status`)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and "authorization" not in request.headers:
            return httpx2.Response(repo_status, json={})
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


async def test_a_repo_the_bot_left_is_kept_without_asking_to_generate():
    seen: list[httpx2.Request] = []
    async with gitea(201, seen, repo_status=200) as http:
        assert not await ensure_repo(http, "https://git.test", "tok", "Meru", "echo", "nuwa/python", "x")
        assert await exists(http, "https://git.test", "Meru", "echo")
    assert seen == []  # never asked with the token: the restricted bot can't see a repo it left


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


def lock_out_gitea(admin: bool | None, rules: list[dict], users: set[str], seen: list[httpx2.Request]):
    """A repo whose bot is admin (True), not (False) or can't see it (None), with these protection rules."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/repos/Meru/echo" and request.method == "GET":
            if admin is None:
                return httpx2.Response(404)
            return httpx2.Response(200, json={"permissions": {"admin": admin}})
        if path.endswith("/branch_protections") and request.method == "GET":
            return httpx2.Response(200, json=rules)
        if path.startswith("/api/v1/users/"):
            return httpx2.Response(200 if path.rsplit("/", 1)[1] in users else 404, json={})
        return httpx2.Response(204 if request.method in ("PUT", "DELETE") else 201, json={})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


@pytest.mark.parametrize(("admin", "expected"), [(True, True), (False, False), (None, False)])
async def test_admin_is_read_from_the_bots_permissions(admin: bool | None, expected: bool):
    async with lock_out_gitea(admin, [], set(), []) as http:
        assert await is_admin(http, "https://git.test", "tok", "Meru", "echo") is expected


async def test_main_gets_a_rule_without_force_push():
    seen: list[httpx2.Request] = []
    async with lock_out_gitea(True, [], set(), seen) as http:
        assert await protect_main(http, "https://git.test", "tok", "Meru", "echo") == "added"
    post = next(r for r in seen if r.method == "POST")
    assert json.loads(post.content) == {"rule_name": "main", "enable_push": True, "enable_force_push": False}


async def test_an_existing_rule_only_loses_force_push():
    seen: list[httpx2.Request] = []
    rules = [{"rule_name": "main", "enable_push": True, "enable_force_push": True}]
    async with lock_out_gitea(True, rules, set(), seen) as http:
        assert await protect_main(http, "https://git.test", "tok", "Meru", "echo") == "force push turned off"
    patch = next(r for r in seen if r.method == "PATCH")
    assert (patch.url.path, json.loads(patch.content)) == (
        "/api/v1/repos/Meru/echo/branch_protections/main",
        {"enable_force_push": False},
    )


async def test_a_rule_without_force_push_is_kept():
    seen: list[httpx2.Request] = []
    rules = [{"rule_name": "main", "enable_force_push": False}]
    async with lock_out_gitea(True, rules, set(), seen) as http:
        assert await protect_main(http, "https://git.test", "tok", "Meru", "echo") == "kept"
    assert [r.method for r in seen] == ["GET"]


async def test_the_merger_gets_write_only_if_it_exists():
    seen: list[httpx2.Request] = []
    async with lock_out_gitea(True, [], {"xolotl"}, seen) as http:
        assert await add_writer(http, "https://git.test", "tok", "Meru", "echo", "xolotl")
        assert not await add_writer(http, "https://git.test", "tok", "Meru", "echo", "nobody")
    [put] = [r for r in seen if r.method == "PUT"]
    assert (put.url.path, json.loads(put.content)) == (
        "/api/v1/repos/Meru/echo/collaborators/xolotl",
        {"permission": "write"},
    )


async def test_stepping_down_leaves_the_bot_read_only():
    seen: list[httpx2.Request] = []
    async with lock_out_gitea(True, [], set(), seen) as http:
        await become_reader(http, "https://git.test", "tok", "Meru", "echo", "shikigami")
    [put] = seen
    assert (put.method, put.url.path) == ("PUT", "/api/v1/repos/Meru/echo/collaborators/shikigami")
    assert json.loads(put.content) == {"permission": "read"}
