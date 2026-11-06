import json
from pathlib import Path

import httpx2

from shikigami.clients import openbao
from shikigami.clients.openbao import KubernetesLogin


def bao(seen: list[dict], lease: int = 600) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx2.Response(200, json={"auth": {"client_token": f"s.{len(seen)}", "lease_duration": lease}})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def test_logs_in_with_the_pod_token_and_keeps_the_session(tmp_path: Path):
    jwt = tmp_path / "token"
    jwt.write_text("jwt-1\n")
    seen: list[dict] = []
    async with bao(seen) as http:
        login = KubernetesLogin(http=http, addr="https://openbao.test", role="meru-shikigami-onboard", jwt_file=jwt)
        assert await login.get() == "s.1"
        assert await login.get() == "s.1"
    assert seen == [{"role": "meru-shikigami-onboard", "jwt": "jwt-1"}]


async def test_logs_in_again_near_the_end_with_the_fresh_pod_token(tmp_path: Path, monkeypatch):
    jwt = tmp_path / "token"
    jwt.write_text("jwt-1")
    now = [1000.0]
    monkeypatch.setattr(openbao.time, "monotonic", lambda: now[0])
    seen: list[dict] = []
    async with bao(seen) as http:
        login = KubernetesLogin(http=http, addr="https://openbao.test", role="r", jwt_file=jwt)
        assert await login.get() == "s.1"
        jwt.write_text("jwt-2")  # the kubelet swapped the file
        now[0] += 600 - openbao.EARLY  # a minute before the session ends
        assert await login.get() == "s.2"
    assert [b["jwt"] for b in seen] == ["jwt-1", "jwt-2"]
