"""The activities of a sync: reading both sides, declaring the requests to Thoth, the app's repo and the dry run."""

import httpx2
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami.activities.base import ActivityBase
from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.repos import NoTemplateError, ensure_repo
from shikigami.sources import declare_apps, read_existing_apps, read_requests


class SyncActivities(ActivityBase):
    """The steps every sync runs, and each app's repo."""

    @activity.defn
    async def read_requests(self) -> RequestsSnapshot:
        """The requests repo's latest commit, parsed and checked."""
        s = self.settings
        return await read_requests(
            self.http, s.gitea_url, s.requests_repo, s.requests_branch, s.gitea_token.get_secret_value()
        )

    @activity.defn
    async def read_existing_apps(self) -> dict[str, ExistingApp]:
        """Every app Thoth knows."""
        return await read_existing_apps(self.http, self.settings.thoth_url, await self.thoth_tokens.get())

    @activity.defn
    async def declare_apps(self, snapshot: RequestsSnapshot) -> dict:
        """Send Thoth the whole request picture; a 403 or 422 isn't retried."""
        try:
            return await declare_apps(self.http, self.settings.thoth_url, await self.thoth_tokens.get(), snapshot)
        except httpx2.HTTPStatusError as e:
            if e.response.status_code in (403, 422):  # no permission, or Thoth refused the snapshot: retries won't help
                raise ApplicationError(
                    f"Thoth said {e.response.status_code}: {e.response.text[:500]}", non_retryable=True
                ) from e
            raise

    @activity.defn
    async def ensure_repo(self, app: str, template: str, description: str) -> bool:
        """Create the app's repo from its template if it doesn't exist; returns whether it was created."""
        s = self.settings
        try:
            return await ensure_repo(
                self.http, s.gitea_url, s.gitea_token.get_secret_value(), s.apps_org, app, template, description
            )
        except NoTemplateError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        except httpx2.HTTPStatusError as e:
            if e.response.status_code in (403, 422):  # the bot may not create here / Gitea refused: retries won't help
                raise ApplicationError(
                    f"Gitea said {e.response.status_code}: {e.response.text[:500]}", non_retryable=True
                ) from e
            raise

    @activity.defn
    async def dry_run(self, action: AppAction) -> None:
        """The steps not built yet: say what would happen. Each real step takes its place, one by one."""
        activity.logger.info("would %s app %s: %s", action.kind, action.app, "; ".join(action.reasons))
