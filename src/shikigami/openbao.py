"""OpenBao's KV v2 store, the few calls Shikigami needs. Every write is check-and-set: it only lands if the entry is
still at the version that was read, so two workflows never overwrite each other's change."""

from dataclasses import dataclass

import httpx2


class Conflict(Exception):
    """The entry changed since it was read; read it again and retry."""


@dataclass
class Entry:
    data: dict[str, str] | None  # None: no entry, or its latest version is deleted
    version: int  # what the next write's cas must be (0 = never written)


class KV:
    def __init__(self, http: httpx2.AsyncClient, addr: str, mount: str, token: str):
        self._http, self._base, self._headers = http, f"{addr}/v1/{mount}/data", {"X-Vault-Token": token}

    async def read(self, path: str) -> Entry:
        response = await self._http.get(f"{self._base}/{path}", headers=self._headers)
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
            f"{self._base}/{path}", headers=self._headers, json={"options": {"cas": cas}, "data": data}
        )
        if response.status_code == 400 and "check-and-set" in response.text:
            raise Conflict(path)
        response.raise_for_status()
