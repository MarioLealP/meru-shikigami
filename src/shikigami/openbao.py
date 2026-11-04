"""OpenBao's KV v2 store, the few calls Shikigami needs.

Every write is check-and-set: it only lands if the entry is still at the version that was read, so two workflows never
overwrite each other's change.
"""

import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx2
from pydantic import BaseModel, ConfigDict, PrivateAttr

from shikigami.incoming import Incoming
from shikigami.tokens import EARLY

Token = Callable[[], Awaitable[str]]


class ConflictError(Exception):
    """The entry changed since it was read; read it again and retry."""


class Entry(BaseModel):
    """One KV entry as read, with the version the next write must name."""

    model_config = ConfigDict(frozen=True)

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


class KubernetesLogin(BaseModel):
    """Shikigami's own OpenBao session (D79).

    Logs in with the pod's service-account token meant for OpenBao (nuwa-app's openbaoLogin, re-read each time - the
    kubelet swaps in a fresh one) and keeps the session until a minute before it ends. Sessions last 10 minutes; what
    they may touch is the role's policy, not anything here.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.AsyncClient
    addr: str
    role: str
    jwt_file: Path
    _token: str | None = PrivateAttr(default=None)  # the cache: the one thing here that changes
    _expires: float = PrivateAttr(default=0.0)

    async def get(self) -> str:
        """A valid OpenBao token, logging in again when the session is nearly over."""
        if self._token is None or time.monotonic() >= self._expires - EARLY:
            jwt = self.jwt_file.read_text().strip()
            response = await self.http.post(
                f"{self.addr}/v1/auth/kubernetes/login", json={"role": self.role, "jwt": jwt}
            )
            response.raise_for_status()
            auth = _Login.model_validate_json(response.content).auth
            self._token = token = auth.client_token
            self._expires = time.monotonic() + auth.lease_duration
            return token
        return self._token


class KV(BaseModel):
    """The KV v2 mount `mount`, with `token` asked for on every call."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.AsyncClient
    addr: str
    mount: str
    token: Token

    @property
    def _base(self) -> str:
        return f"{self.addr}/v1/{self.mount}/data"

    async def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": await self.token()}

    async def read(self, path: str) -> Entry:
        """The latest version of `path`; a missing or deleted entry comes back with `data` None."""
        response = await self.http.get(f"{self._base}/{path}", headers=await self._headers())
        if response.status_code == 404:
            # a deleted latest version still answers 404, but with its metadata - the cas needs that version
            found = _Read.model_validate_json(response.content) if response.content else _Read()
            return Entry(data=None, version=found.data.metadata.version)
        response.raise_for_status()
        found = _Read.model_validate_json(response.content)
        return Entry(data=found.data.data, version=found.data.metadata.version)

    async def write(self, path: str, data: dict[str, str], cas: int) -> None:
        """Write `path` if it's still at version `cas`, else raise ConflictError."""
        response = await self.http.post(
            f"{self._base}/{path}", headers=await self._headers(), json={"options": {"cas": cas}, "data": data}
        )
        if response.status_code == 400 and "check-and-set" in response.text:
            raise ConflictError(path)
        response.raise_for_status()
