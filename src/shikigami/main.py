"""`shikigami-run`: one process per pod - the Temporal worker and the HTTP side, side by side (D75). Replicas are
identical; Temporal hands each task to one of them."""

import asyncio

import httpx2
import uvicorn
from temporalio.worker import Worker

from shikigami.activities import Activities
from shikigami.config import Settings
from shikigami.temporal import connect, ensure_nightly, keep_token_fresh
from shikigami.tokens import TokenSource
from shikigami.web import make_app
from shikigami.workflows import AppWorkflow, RequestsSync


async def run() -> None:
    settings = Settings()  # the secrets come from the environment
    async with httpx2.AsyncClient(timeout=30) as http:

        def tokens(client_id: str) -> TokenSource:
            password = settings.authentik_password
            return TokenSource(http, settings.token_url, client_id, settings.authentik_username, password)

        temporal_tokens = tokens(settings.temporal_client_id)
        client = await connect(settings, temporal_tokens)
        await ensure_nightly(client, settings)
        activities = Activities(settings, http, tokens(settings.thoth_client_id))
        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[RequestsSync, AppWorkflow],
            activities=[activities.read_requests, activities.read_existing_apps, activities.dry_run],
            max_concurrent_activities=settings.max_concurrent_activities,
        )
        server = uvicorn.Server(uvicorn.Config(make_app(settings, client), host="0.0.0.0", port=settings.port))
        async with asyncio.TaskGroup() as group:
            group.create_task(keep_token_fresh(client, temporal_tokens))
            group.create_task(worker.run())
            group.create_task(server.serve())


def main() -> None:
    asyncio.run(run())
