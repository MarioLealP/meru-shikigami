"""Temporal activities: the only places Shikigami touches the outside world. Each one may retry, so each is safe to
run twice."""

import httpx2
from temporalio import activity

from shikigami.config import Settings
from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.sources import read_existing_apps, read_requests
from shikigami.tokens import TokenSource


class Activities:
    def __init__(self, settings: Settings, http: httpx2.AsyncClient, thoth_tokens: TokenSource):
        self._settings, self._http, self._thoth_tokens = settings, http, thoth_tokens

    @activity.defn
    async def read_requests(self) -> RequestsSnapshot:
        s = self._settings
        return await read_requests(
            self._http, s.gitea_url, s.requests_repo, s.requests_branch, s.gitea_token.get_secret_value()
        )

    @activity.defn
    async def read_existing_apps(self) -> dict[str, ExistingApp]:
        return await read_existing_apps(self._http, self._settings.thoth_url, await self._thoth_tokens.get())

    @activity.defn
    async def dry_run(self, action: AppAction) -> None:
        """Step 3 stops here: say what would happen. Later steps replace this with the real work, step by step."""
        activity.logger.info("would %s app %s: %s", action.kind, action.app, "; ".join(action.reasons))
