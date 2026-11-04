"""The activities of the retire pass (D85) and the namespace mark a restore takes off (D106).

In this order, each step keeping what's done: stop -> wait for Argo -> park the marker -> removal PRs -> revoke runner
(+ the Zot login, BuildActivities) -> archive. Deletes nothing: Charon purges later.
"""

from datetime import datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami import marker, retire, trust
from shikigami.activities.base import ActivityBase
from shikigami.activities.texts import PARK, STOP, retire_title
from shikigami.argo import application, not_stopped
from shikigami.namespaces import set_removed_at, stamp
from shikigami.proposals import Edit
from shikigami.repos import archive, change, commit_files, delete_runners, head, is_admin, read_file
from shikigami.sources import mark_retire_step


class RetireActivities(ActivityBase):
    """Taking an app out of service, one safe-to-repeat step at a time."""

    @activity.defn
    async def retire_stop(self, app: str) -> str:
        """Stop the app: `enabled: false` (no workload) + `build: false` (no runner).

        One commit while Shikigami still administers the repo, else a PR Xolotl merges (cm-konk) - "proposed
        in"/"waiting on" means the pass stops here until it's merged. Kept once both are there, or once the marker is
        parked (a later pass).
        """
        s = self.settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self.http, s.gitea_url, token, org, app):
            return await self._ping_merger(await self._stop_pr(app))
        marker_file = await read_file(self.http, s.gitea_url, token, org, app, "deploy/meru.yaml")
        if marker_file is None:
            return "kept (marker parked)"
        values = await read_file(self.http, s.gitea_url, token, org, app, "deploy/values.yaml")
        if values is None:
            raise ApplicationError(f"Meru/{app} has no deploy/values.yaml", non_retryable=True)
        if retire.is_stopped(values[0], marker_file[0]):
            return "kept"
        try:
            files = [
                change("update", "deploy/values.yaml", retire.stopped_values(values[0]), sha=values[1]),
                change("update", "deploy/meru.yaml", retire.stopped_marker(marker_file[0]), sha=marker_file[1]),
            ]
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        message = (
            f"chore(deploy): stop {app} - retired (D85)\n\n"
            "Removed by its request: no workload, no build runner. Shikigami."
        )
        await commit_files(self.http, s.gitea_url, token, org, app, message, files)
        return "written"

    async def _stop_pr(self, app: str) -> str:
        marker_text = await self._public(app, "deploy/meru.yaml")
        if marker_text is None:
            return "kept (marker parked)"
        values = await self._public(app, "deploy/values.yaml")
        if values is None:
            raise ApplicationError(f"Meru/{app} has no deploy/values.yaml", non_retryable=True)
        if retire.is_stopped(values, marker_text):
            return "kept"
        try:
            files = {
                "deploy/values.yaml": Edit(before=values, after=retire.stopped_values(values)),
                "deploy/meru.yaml": Edit(before=marker_text, after=retire.stopped_marker(marker_text)),
            }
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        title, again = retire_title("stop", app), [marker.TITLE.format(app=app)]  # a newer marker PR: retired again
        return await self._open_once(app, title, [title, *again], f"retire-stop-{app}", STOP.format(app=app), files)

    async def _park_pr(self, app: str) -> str:
        marker_text = await self._public(app, "deploy/meru.yaml")
        if marker_text is None:
            return "kept"
        template = await self._public(app, "deploy/meru.yaml.in")
        try:
            files = {
                "deploy/meru.yaml.in": Edit(before=template, after=retire.parked(marker_text)),
                "deploy/meru.yaml": Edit(before=marker_text, after=None),
            }
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        title, again = retire_title("park", app), [marker.TITLE.format(app=app)]
        return await self._open_once(app, title, [title, *again], f"retire-park-{app}", PARK.format(app=app), files)

    @activity.defn
    async def retire_wait_stopped(self, app: str) -> str:
        """Until Argo has synced the repo's current main with no workload left - retried by the workflow (30 s apart).

        No Application = already parked and gone.
        """
        s = self.settings
        if self.kube is None:
            raise ApplicationError("no Kubernetes API client (kubernetesApi off?)", non_retryable=True)
        found = await application(
            self.kube, s.kubernetes_url, s.kubernetes_dir / "token", s.argocd_namespace, f"app-{app}"
        )
        if found is None:
            return "gone"
        commit = await head(self.http, s.gitea_url, None, s.apps_org, app)  # public: Shikigami may have left (D89)
        reason = not_stopped(found, commit)
        if reason:
            raise ApplicationError(f"app-{app} not stopped yet: {reason}")
        return "stopped"

    @activity.defn
    async def retire_park_marker(self, app: str) -> str:
        """deploy/meru.yaml -> deploy/meru.yaml.in: the ApplicationSet drops the app.

        Only after retire_wait_stopped. One commit while Shikigami administers the repo, else a PR Xolotl merges
        (cm-konk).
        """
        s = self.settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self.http, s.gitea_url, token, org, app):
            return await self._ping_merger(await self._park_pr(app))
        marker_file = await read_file(self.http, s.gitea_url, token, org, app, "deploy/meru.yaml")
        if marker_file is None:
            return "kept"
        try:
            files = [
                change("create", "deploy/meru.yaml.in", retire.parked(marker_file[0])),
                change("delete", "deploy/meru.yaml", sha=marker_file[1]),
            ]
        except trust.BadLayoutError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        message = (
            f"chore(deploy): park {app}'s marker - retired (D85)\n\n"
            "Meru stops deploying the repo; onboarding again undoes it."
        )
        await commit_files(self.http, s.gitea_url, token, org, app, message, files)
        return "parked"

    @activity.defn
    async def retire_runner(self, app: str) -> int:
        """Remove the repo's runners from Gitea (the runner pod is already gone with build: false).

        Needs repo admin: in a repo Shikigami left (D89) the offline record stays for Charon to clear at purge
        (cm-konk).
        """
        s = self.settings
        token = s.gitea_token.get_secret_value()
        if not await is_admin(self.http, s.gitea_url, token, s.apps_org, app):
            activity.logger.info("%s: runner records left to Charon (Shikigami isn't admin)", app)
            return 0
        return await delete_runners(self.http, s.gitea_url, token, s.apps_org, app)

    @activity.defn
    async def retire_mark(self, app: str, status: str) -> str:
        """Thoth's side of the pass (D85): decommissioning at its start, retained at its end."""
        token = await self.thoth_tokens.get()
        return await mark_retire_step(self.http, self.settings.thoth_url, token, app, status)

    async def _set_removed_at(self, app: str, value: str | None) -> list[str]:
        """The app's two namespaces, `<app>` and `<app>-build`; returns the ones that exist (and were set)."""
        s = self.settings
        if self.kube is None:
            raise ApplicationError("no Kubernetes API client (kubernetesApi off?)", non_retryable=True)
        token = s.kubernetes_dir / "token"
        return [
            ns for ns in (app, f"{app}-build") if await set_removed_at(self.kube, s.kubernetes_url, token, ns, value)
        ]

    @activity.defn
    async def retire_mark_namespaces(self, app: str, removed_at: datetime | None) -> list[str]:
        """Charon step 1 (D107): put the request's removedAt on the app's namespaces.

        Charon deletes only marked ones, after its own 30 days. Last before Thoth hears retained: a marked namespace is
        a fully retired app's.
        """
        if removed_at is None:
            raise ApplicationError(
                f"{app}'s request has no removedAt - Xolotl stamps it on the removal merge", non_retryable=True
            )
        return await self._set_removed_at(app, stamp(removed_at))

    @activity.defn
    async def restore_unmark_namespaces(self, app: str) -> list[str]:
        """A restore (D106): the mark comes off first, so Charon never deletes a restored app's namespaces.

        Argo doesn't remove an annotation it didn't write - this does.
        """
        return await self._set_removed_at(app, None)

    @activity.defn
    async def retire_archive(self, app: str) -> bool:
        """Archive the repo - last: nothing is written to it after this.

        Needs repo admin: a repo Shikigami left (D89) is archived by Charon at purge instead (cm-konk).
        """
        s = self.settings
        token = s.gitea_token.get_secret_value()
        if not await is_admin(self.http, s.gitea_url, token, s.apps_org, app):
            activity.logger.info("%s: archiving left to Charon (Shikigami isn't admin)", app)
            return False
        return await archive(self.http, s.gitea_url, token, s.apps_org, app)
