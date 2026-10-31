"""OpenBao's KV v2 store, the few calls Shikigami needs. Every write is check-and-set: it only lands if the entry is
still at the version that was read, so two workflows never overwrite each other's change."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2

from shikigami.tokens import EARLY

Token = Callable[[], Awaitable[str]]


class Conflict(Exception):
    """The entry changed since it was read; read it again and retry."""


@dataclass
class Entry:
    data: dict[str, str] | None  # None: no entry, or its latest version is deleted
    version: int  # what the next write's cas must be (0 = never written)


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
            auth = response.json()["auth"]
            self._token = token = str(auth["client_token"])
            self._expires = time.monotonic() + float(auth["lease_duration"])
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
            body = response.json() if response.content else {}
            metadata = (body.get("data") or {}).get("metadata") or {}
            return Entry(None, metadata.get("version", 0))
        response.raise_for_status()
        data = response.json()["data"]
        return Entry(data["data"], data["metadata"]["version"])

    async def write(self, path: str, data: dict[str, str], cas: int) -> None:
        response = await self._http.post(
            f"{self._base}/{path}", headers=await self._headers(), json={"options": {"cas": cas}, "data": data}
        )
        if response.status_code == 400 and "check-and-set" in response.text:
            raise Conflict(path)
        response.raise_for_status()
