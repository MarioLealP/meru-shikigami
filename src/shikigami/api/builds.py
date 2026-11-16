"""Each app's waiting build jobs - what its one-job build runners (sindri-build, ephemeral) scale on through KEDA."""

from typing import Annotated

import httpx2
from fastapi import APIRouter, HTTPException, Path

from shikigami.clients.repos import waiting_jobs
from shikigami.config import Settings

AppName = Annotated[str, Path(pattern=r"^[a-z]([-a-z0-9]{0,40}[a-z0-9])?$")]


def builds_router(settings: Settings, http: httpx2.AsyncClient) -> APIRouter:
    """GET /builds/<app>/waiting -> {"waiting": n}: the app's Actions jobs waiting for a runner."""
    router = APIRouter()

    @router.get("/builds/{app_name}/waiting", status_code=200, responses={502: {"description": "Can't read Gitea"}})
    async def get_waiting_builds(app_name: AppName) -> dict[str, int]:
        token = settings.gitea_token.get_secret_value()
        try:
            waiting = await waiting_jobs(http, settings.gitea_url, token, settings.apps_org, app_name)
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"can't read Gitea: {type(e).__name__}") from e
        return {"waiting": waiting}

    return router
