"""Shikigami's HTTP side: probes, the webhook ping (D65) and the backlog KEDA scales on (D75)."""

import hashlib
import hmac

from fastapi import FastAPI, Header, HTTPException, Request
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from shikigami.config import Settings
from shikigami.temporal import backlog
from shikigami.workflows import SYNC_ID, RequestsSync


def make_app(settings: Settings, client: Client) -> FastAPI:
    app = FastAPI(title="Shikigami", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/heartbeat")
    async def heartbeat() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        # Temporal's own health check needs no token; it proves the TLS connection works.
        try:
            healthy = await client.service_client.check_health()
        except RPCError:
            healthy = False
        if not healthy:
            raise HTTPException(503, "Temporal not reachable")
        return {"status": "ok"}

    @app.post("/ping", status_code=202)
    async def ping(request: Request, x_gitea_signature: str | None = Header(default=None)) -> dict[str, str]:
        """Go look at the requests repo. The body is never read for meaning (D65), only to check the signature."""
        if settings.webhook_secret is not None:
            expected = hmac.new(
                settings.webhook_secret.get_secret_value().encode(), await request.body(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, x_gitea_signature or ""):
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

    @app.get("/backlog")
    async def get_backlog() -> dict[str, int]:
        return {"backlog": await backlog(client, settings)}

    return app
