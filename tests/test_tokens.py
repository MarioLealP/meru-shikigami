"""The Authentik login is the pod's Kubernetes token (D104): sent as the client assertion, re-read each time."""

from pathlib import Path

import httpx2

from shikigami.tokens import ASSERTION, TokenSource


async def test_logs_in_with_the_pod_token_and_no_password(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("first\n")
    forms: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        forms.append(dict(httpx2.QueryParams(request.content.decode())))
        return httpx2.Response(200, json={"access_token": f"t{len(forms)}", "expires_in": 0})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        tokens = TokenSource(http=http, token_url="https://auth.test/token/", client_id="thoth", token_file=token_file)
        assert await tokens.get() == "t1"
        token_file.write_text("second\n")  # the kubelet swapped in a fresh one
        assert await tokens.get() == "t2"  # expires_in 0: used up at once

    assert forms[0] == {
        "grant_type": "client_credentials",
        "client_id": "thoth",
        "scope": "thoth",
        "client_assertion_type": ASSERTION,
        "client_assertion": "first",
    }
    assert forms[1]["client_assertion"] == "second"
