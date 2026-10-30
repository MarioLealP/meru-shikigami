"""Short-lived Authentik tokens (client_credentials), cached until a minute before they expire."""

import time

import httpx2
from pydantic import SecretStr

EARLY = 60  # seconds before expiry when a token counts as used up


class TokenSource:
    """One provider's tokens for one service account. Tokens last 10 minutes (thoth.yaml, temporal.yaml)."""

    def __init__(self, http: httpx2.AsyncClient, token_url: str, client_id: str, username: str, password: SecretStr):
        self._http, self._url, self._client_id = http, token_url, client_id
        self._username, self._password = username, password
        self._token: str | None = None
        self._expires = 0.0

    async def get(self) -> str:
        if self._token is None or time.monotonic() >= self._expires - EARLY:
            response = await self._http.post(
                self._url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "username": self._username,
                    "password": self._password.get_secret_value(),
                    "scope": self._client_id,  # the provider's own scope mapping (temporal / thoth permissions)
                },
            )
            response.raise_for_status()
            body = response.json()
            self._token = token = str(body["access_token"])
            self._expires = time.monotonic() + float(body.get("expires_in", 600))
            return token
        return self._token
