"""OpenBao's KV v2 store, the few calls Shikigami needs. Every write is check-and-set: it only lands if the entry is
still at the version that was read, so two workflows never overwrite each other's change."""

import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx2
from pydantic import BaseModel

from shikigami.incoming import Incoming
from shikigami.tokens import EARLY

Token = Callable[[], Awaitable[str]]


class Conflict(Exception):
    """The entry changed since it was read; read it again and retry."""


class Entry(BaseModel):
    data: dict[str, str] | None  # None: no entry, or its latest version is deleted
    version: int  # what the next write's cas must be (0 = never written)


# OpenBao's answers - only the fields Shikigami reads.
class _Auth(Incoming):
    client_token: str
    lease_duration: int


class _Login(Incoming):
    auth: _Auth


class _Metadata(Incoming):
    version: int = 0


class _Secret(Incoming):
    data: dict[str, str] | None = None
    metadata: _Metadata = _Metadata()


class _Read(Incoming):
    data: _Secret = _Secret()


class KubernetesLogin:
    """Shikigami's own OpenBao session (D79): logs in with the pod's service-account token meant for OpenBao (nuwa-app's
    openbaoLogin, re-read each time - the kubelet swaps in a fresh one) and keeps the session until a minute before
    it ends. Sessions last 10 minutes; what they may touch is the role's policy, not anything here."""

    def __init__(self, http: httpx2.AsyncClient, addr: str, role: str, jwt_file: Path):
        self._http, self._url, self._role, self._jwt_file = http, f"{addr}/v1/auth/kubernetes/login", role, jwt_file
        self._token: str | None = None
        self._expires = 0.0

    async def get(self) -> str:
        if self._token is None or time.monotonic() >= self._expires - EARLY:
            jwt = self._jwt_file.read_text().strip()
            response = await self._http.post(self._url, json={"role": self._role, "jwt": jwt})
            response.raise_for_status()
            auth = _Login.model_validate_json(response.content).auth
            self._token = token = auth.client_token
            self._expires = time.monotonic() + auth.lease_duration
            return token
        return self._token


class KV:
    def __init__(self, http: httpx2.AsyncClient, addr: str, mount: str, token: Token):
        self._http, self._base, self._token = http, f"{addr}/v1/{mount}/data", token

    async def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": await self._token()}

    async def read(self, path: str) -> Entry:
        response = await self._http.get(f"{self._base}/{path}", headers=await self._headers())
        if response.status_code == 404:
            # a deleted latest version still answers 404, but with its metadata - the cas needs that version
            found = _Read.model_validate_json(response.content) if response.content else _Read()
            return Entry(data=None, version=found.data.metadata.version)
        response.raise_for_status()
        found = _Read.model_validate_json(response.content)
        return Entry(data=found.data.data, version=found.data.metadata.version)

    async def write(self, path: str, data: dict[str, str], cas: int) -> None:
        response = await self._http.post(
            f"{self._base}/{path}", headers=await self._headers(), json={"options": {"cas": cas}, "data": data}
        )
        if response.status_code == 400 and "check-and-set" in response.text:
            raise Conflict(path)
        response.raise_for_status()
