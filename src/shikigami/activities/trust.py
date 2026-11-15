"""The activities that propose an app's trust: its fence and push rule - in, and out again."""

from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami.activities.base import ActivityBase
from shikigami.clients.proposals import Edit
from shikigami.clients.repos import public_file
from shikigami.core import trust


class TrustActivities(ActivityBase):
    """The trust PRs into Meru/meru and Meru/zot (D82)."""

    @activity.defn
    async def ensure_trust(
        self, app: str, description: str, public_key: str, environments: list[str] | None = None
    ) -> str:
        """Propose the app's fence and push rule as PRs (D82).

        They go to Meru/meru and Meru/zot - Shikigami only reads those repos. Each repo: kept if the app
        is already there, else one PR, never a second one while a PR with the same title exists (open, merged, or closed
        unmerged - a rejection isn't re-proposed). Another entry already there for the app is left for a person.
        Environments (D126) widen the fence; `None` is a caller from before them (a replayed workflow). `public_key` is
        kept for workflows started before Kyverno was dropped (2026-11-15) - nothing trusts a signing key any more.
        """
        environments = environments or []
        try:
            changes = [
                ("fence", "meru", "fence", await self._fence_files(app, description, environments)),
                ("push rule", "zot", "push rule", await self._push_rule_files(app)),
            ]
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        parts = []
        for label, repo, what, files in changes:
            if isinstance(files, trust.ConflictError):
                parts.append(f"{label} waiting: {files} - by hand")
            else:
                parts.append(f"{label} {await self._propose(app, repo, what, files)}")
        return await self._ping_merger("; ".join(parts))

    async def _fence_files(self, app: str, description: str, environments: list[str]) -> dict[str, Edit]:
        org, path = self.settings.apps_org, f"projects/{app}.yaml"
        if await public_file(self.http, self.settings.gitea_url, org, "meru", path) is not None:
            return {}
        kustomization = await self._required(org, "meru", "kustomization.yaml")
        return {
            path: Edit(before=None, after=trust.fence(app, description, environments)),
            "kustomization.yaml": Edit(
                before=kustomization, after=trust.with_resource(kustomization, "projects", path)
            ),
        }

    async def _push_rule_files(self, app: str) -> dict[str, Edit] | trust.ConflictError:
        current = await self._required(self.settings.apps_org, "zot", "config.json")
        try:
            updated = trust.with_push_rule(current, app)
        except trust.ConflictError as e:
            return e
        return {"config.json": Edit(before=current, after=updated)} if updated != current else {}

    @activity.defn
    async def retire_trust(self, app: str, environments: list[str] | None = None) -> str:
        """Removal PRs for the fence and the push rule - only this app's entries (Meru/kyverno went 2026-11-15)."""
        org = self.settings.apps_org
        fence = f"projects/{app}.yaml"
        try:
            meru: dict[str, Edit] = {}
            if (fence_text := await public_file(self.http, self.settings.gitea_url, org, "meru", fence)) is not None:
                kustomization = await self._required(org, "meru", "kustomization.yaml")
                meru = {
                    fence: Edit(before=fence_text, after=None),
                    "kustomization.yaml": Edit(
                        before=kustomization, after=trust.without_resource(kustomization, fence)
                    ),
                }
            config = await self._required(org, "zot", "config.json")
            zot: dict[str, Edit] = (
                {"config.json": Edit(before=config, after=rule)}
                if (rule := trust.without_push_rule(config, app)) != config
                else {}
            )
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        return await self._ping_merger(
            f"fence {await self._propose(app, 'meru', 'fence', meru, 'retire')}; "
            f"push rule {await self._propose(app, 'zot', 'push rule', zot, 'retire')}"
        )
