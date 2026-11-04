"""Shikigami's HTTP side.

Probes, the webhook ping (D65), the backlog KEDA scales on (D75) and the request check Janus asks before opening a
request PR (D96).
"""

import hashlib
import hmac
from typing import Annotated

import httpx2
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import SecretStr
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from shikigami.config import Settings
from shikigami.sources import Proposal, Verdict, check_proposal
from shikigami.temporal import backlog
from shikigami.workflows import SYNC_ID, RequestsSync


def _signed(secret: SecretStr, body: bytes, signature: str | None) -> bool:
    """Whether `signature` is Gitea's HMAC-SHA256 of the body."""
    expected = hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def make_app(settings: Settings, client: Client, http: httpx2.AsyncClient) -> FastAPI:
    """The FastAPI app: probes, /ping, /validate and /backlog."""
    app = FastAPI(title="Shikigami", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/heartbeat", status_code=200)
    async def heartbeat() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", status_code=200, responses={503: {"description": "Temporal not reachable"}})
    async def ready() -> dict[str, str]:
        # Temporal's own health check needs no token; it proves the TLS connection works.
        try:
            healthy = await client.service_client.check_health()
        except RPCError:
            healthy = False
        if not healthy:
            raise HTTPException(503, "Temporal not reachable")
        return {"status": "ok"}

    @app.post("/ping", status_code=202, responses={401: {"description": "Bad signature"}})
    async def ping(request: Request, x_gitea_signature: Annotated[str | None, Header()] = None) -> dict[str, str]:
        """Go look at the requests repo. The body is never read for meaning (D65), only to check the signature."""
        secret = settings.webhook_secret
        if secret is not None and not _signed(secret, await request.body(), x_gitea_signature):
            raise HTTPException(401, "bad signature")
        # Start requests-sync, or poke the running one: at most one run + one queued pass.
        await client.start_workflow(
            RequestsSync.run,
            id=SYNC_ID,
            task_queue=settings.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            start_signal="poke",
        )
        return {"status": "queued"}

    @app.post("/validate", status_code=200, responses={502: {"description": "Can't read the requests repo"}})
    async def validate(proposal: Proposal) -> Verdict:
        """Would this request file be valid on the requests repo's main?

        Parsing only (yaml.safe_load + the request models); nothing is written anywhere. Reachable from Janus only (its
        NetworkPolicy).
        """
        s = settings
        try:
            return await check_proposal(
                http, s.gitea_url, s.requests_repo, s.requests_branch, s.gitea_token.get_secret_value(), proposal
            )
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"can't read the requests repo: {type(e).__name__}") from e

    @app.get("/backlog", status_code=200)
    async def get_backlog() -> dict[str, int]:
        return {"backlog": await backlog(client, settings)}

    return app
