import httpx2
import pytest
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from shikigami.activities import Activities
from shikigami.config import Settings
from shikigami.models import RequestsSnapshot
from shikigami.tokens import TokenSource


def activities(status: int) -> Activities:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "auth.test":
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 600})
        return httpx2.Response(status, json={"title": "Snapshot refused"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tokens = TokenSource(http, "https://auth.test/token/", "thoth", "shikigami", SecretStr("pw"))
    return Activities(
        Settings.model_construct(thoth_url="http://thoth.test", gitea_token=SecretStr("tok")), http, tokens
    )


SNAPSHOT = RequestsSnapshot(commit="c1", apps={}, databases={}, problems=[])


@pytest.mark.parametrize("status", [403, 422])
async def test_a_refused_snapshot_is_not_retried(status: int):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities(status).declare_apps, SNAPSHOT)
    assert raised.value.non_retryable
    assert str(status) in str(raised.value)


async def test_a_server_error_is_retried():
    with pytest.raises(httpx2.HTTPStatusError):
        await ActivityEnvironment().run(activities(503).declare_apps, SNAPSHOT)


@pytest.mark.parametrize("status", [403, 404, 422])
async def test_repo_refusals_are_not_retried(status: int):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities(status).ensure_repo, "echo", "nuwa/python", "x")
    assert raised.value.non_retryable


async def test_a_git_url_template_is_not_retried():
    with pytest.raises(ApplicationError, match="only nuwa/<name>"):
        await ActivityEnvironment().run(activities(201).ensure_repo, "echo", "https://git.example/x.git", "x")
