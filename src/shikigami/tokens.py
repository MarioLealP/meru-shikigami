"""Short-lived Authentik tokens (client_credentials), cached until a minute before they expire.

The login is the pod's own Kubernetes token (D104: nuwa-app authentikLogin, audience authentik, kubelet-refreshed),
sent as the client assertion - no password anywhere. Authentik checks it against k3s's key and answers with the
rights of the groups that list this workload (blueprints shikigami.yaml).
"""

import time
from pathlib import Path

import httpx2
from pydantic import BaseModel, ConfigDict, PrivateAttr

from shikigami.incoming import Incoming

ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
EARLY = 60  # seconds before expiry when a token counts as used up


class _TokenAnswer(Incoming):
    access_token: str
    expires_in: int = 600


class TokenSource(BaseModel):
    """One provider's tokens for this pod's workload. Tokens last 10 minutes (thoth.yaml, temporal.yaml)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    http: httpx2.AsyncClient
    token_url: str
    client_id: str
    token_file: Path
    _token: str | None = PrivateAttr(default=None)  # the cache: the one thing here that changes
    _expires: float = PrivateAttr(default=0.0)

    async def get(self) -> str:
        """A valid access token, fetching a new one when the cached one is nearly used up."""
        if self._token is None or time.monotonic() >= self._expires - EARLY:
            response = await self.http.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "scope": self.client_id,  # the provider's own scope mapping (temporal / thoth permissions)
                    "client_assertion_type": ASSERTION,
                    # re-read each time: the kubelet swaps in a fresh token before the old one runs out
                    "client_assertion": self.token_file.read_text().strip(),
                },
            )
            response.raise_for_status()
            body = _TokenAnswer.model_validate_json(response.content)
            self._token = token = body.access_token
            self._expires = time.monotonic() + body.expires_in
            return token
        return self._token
