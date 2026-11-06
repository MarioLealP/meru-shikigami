"""The activities that end onboarding: the deploy files, the first build and Shikigami's lock-out."""

from collections.abc import Mapping

from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami.activities.base import ActivityBase
from shikigami.activities.texts import DEPLOY_PR, retire_title
from shikigami.clients import glitchtip
from shikigami.clients.proposals import Edit
from shikigami.clients.repos import (
    add_writer,
    become_reader,
    dispatched_run,
    is_admin,
    protect_main,
    public_file,
    read_file,
    rerun_failed,
    run_workflow,
)
from shikigami.core import marker, retire


def _deploy_files(
    template: str,
    values: str | None,
    owner: str,
    secrets: list[str],
    entries: Mapping[str, str] | None = None,
    dsn: str | None = None,
) -> dict[str, Edit]:
    """The marker from the template, and deploy/values.yaml wired if that changes it.

    The owner, the GlitchTip DSN (D125) and the request's secrets.
    """
    files = {
        "deploy/meru.yaml": Edit(before=None, after=marker.with_owner(template, owner, "deploy/meru.yaml.in")),
        "deploy/meru.yaml.in": Edit(before=template, after=None),
    }
    if values is None:
        if secrets:
            raise marker.BadTemplateError("no deploy/values.yaml to wire the request's secrets into")
        return files
    text = retire.restarted_values(values)  # a restore (D106): the stop's line goes with the park
    if marker.owner_unset(text):
        text = marker.with_owner(text, owner, "deploy/values.yaml")
    text = marker.with_glitchtip(text, dsn)
    text = marker.with_secrets(text, secrets, "deploy/values.yaml", entries)
    if text != values:
        files["deploy/values.yaml"] = Edit(before=values, after=text)
    return files


class DeployActivities(ActivityBase):
    """The last onboarding steps, in the order they run (D81, D89, D90)."""

    @activity.defn
    async def ensure_glitchtip(self, app: str) -> str | None:
        """The app's GlitchTip project, made if missing, and its DSN (D125); None without a GlitchTip token.

        Runs every pass, so apps onboarded before D125 get their project too. Their repo is theirs (D89): the DSN goes
        in by their own PR, so a project made here has its DSN logged.
        """
        s = self.settings
        if s.glitchtip_token is None:
            return None
        try:
            dsn, created = await glitchtip.project_dsn(
                self.http, s.glitchtip_url, s.glitchtip_token, s.glitchtip_org, s.glitchtip_team, app
            )
        except ValueError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        if created:
            activity.logger.info("app %s: GlitchTip project created, DSN %s", app, dsn)
        return dsn

    @activity.defn
    async def ensure_deploy_files(
        self,
        app: str,
        owner: str,
        public_key: str,
        secrets: list[str] | None = None,
        entries: dict[str, str] | None = None,
        dsn: str | None = None,
    ) -> str:
        """Propose deploy/meru.yaml, the marker that makes Meru deploy the repo (D81).

        Made from the template's deploy/meru.yaml.in with the request's owner, plus the owner and the request's secrets
        wired into deploy/values.yaml (D99), as one PR Xolotl rebuilds and merges (D99: no Shikigami write reaches a
        main unchecked). "kept" once the marker is on main: the repo is the app's then, never touched again. Waits (says
        why, the next sync tries again) until the app's fence and signing key are in place - a marker before them would
        deploy into nothing - and while the PR is open.
        """
        s = self.settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        # Read like Argo reads it (Meru/meru deploys public repos only), not with the bot's token: the restricted bot
        # can't see repos it didn't create, and those are exactly the ones that already have a marker.
        if await public_file(self.http, s.gitea_url, org, app, "deploy/meru.yaml") is not None:
            return "kept"
        if await public_file(self.http, s.gitea_url, org, "meru", f"projects/{app}.yaml") is None:
            return f"waiting: no fence Meru/meru projects/{app}.yaml"
        keys = await public_file(self.http, s.gitea_url, org, "kyverno", "keys/meru-signing-keys.yaml")
        if keys is None or not marker.key_trusted(keys, app, public_key):
            return f"waiting: Meru/kyverno doesn't trust {app}'s signing key yet"
        template = await read_file(self.http, s.gitea_url, token, org, app, "deploy/meru.yaml.in")
        if template is None:
            return "waiting: no deploy/meru.yaml.in (repo generated before the template had it)"
        values = await read_file(self.http, s.gitea_url, token, org, app, "deploy/values.yaml")
        try:
            files = _deploy_files(template[0], values[0] if values else None, owner, secrets or [], entries, dsn)
        except marker.BadTemplateError as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        # Xolotl merges it: Write now, not only at the lock-out (D89). A repo Shikigami already left (a restore, D106)
        # kept Xolotl's Write from the first onboarding - and Shikigami couldn't give it anyway.
        if await is_admin(self.http, s.gitea_url, token, org, app) and not await add_writer(
            self.http, s.gitea_url, token, org, app, s.merger_user
        ):
            return f"waiting: no {s.merger_user} user to merge the marker PR"
        title = marker.TITLE.format(app=app)
        opened = await self._open_once(
            app,
            title,
            [title, retire_title("park", app)],  # after a retire, the newer park PR means: propose the marker again
            f"deploy-{app}",
            DEPLOY_PR.format(
                app=app,
                owner=owner,
                secrets=", ".join(f"`{marker.env_name(n)}`" for n in secrets or []) or "none",
            ),
            files,
        )
        return await self._ping_merger(opened)  # Xolotl sweeps now, not at its 15-minute backstop

    @activity.defn
    async def first_build(self, app: str) -> str:
        """D90: the app's first build, before Shikigami leaves the repo (after that nobody automated can re-run it).

        None yet -> started (D99: here, once the marker PR is merged). Passed -> done. Failed -> its failed jobs re-run,
        up to first_build_reruns times (Gitea counts the attempts, so nothing is remembered here). Still running, just
        started or just re-run -> an error, and the workflow's retry policy asks again a minute later. Not admin any
        more -> nothing to wait for.
        """
        s = self.settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self.http, s.gitea_url, token, org, app):
            return "not watched (Shikigami already left the repo)"
        run = await dispatched_run(self.http, s.gitea_url, token, org, app)
        if run is None:
            # Asked for here, not with the marker: the marker is a PR now (D99), merged whenever Xolotl gets to it.
            # CI ignores deploy/**-only pushes, so the merge starts nothing by itself.
            await run_workflow(self.http, s.gitea_url, token, org, app, "ci.yaml")
            activity.logger.info("%s: first build started", app)
            raise ApplicationError("first build started")
        attempt = f"attempt {run.run_attempt}"
        if run.status != "completed":
            raise ApplicationError(f"first build {run.status} ({attempt})")
        if run.conclusion == "success":
            return f"passed ({attempt})"
        if run.run_attempt > s.first_build_reruns:
            return f"{run.conclusion} after {run.run_attempt} attempts - left to the owner"
        await rerun_failed(self.http, s.gitea_url, token, org, app, run.id)
        activity.logger.warning("%s: first build %s (%s), failed jobs re-run", app, run.conclusion, attempt)
        raise ApplicationError(f"first build {run.conclusion} ({attempt}), re-run")

    @activity.defn
    async def lock_out(self, app: str) -> str:
        """The last onboarding step (D89), once the marker is in.

        Protect main (force push off), give the merger Write, and drop Shikigami to Read - it can still propose (retire
        PRs, cm-konk), never push. Kept once Shikigami isn't admin there any more - whoever made the repo or after it
        stepped down.
        """
        s = self.settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self.http, s.gitea_url, token, org, app):
            return "kept"
        protected = await protect_main(self.http, s.gitea_url, token, org, app)
        merger = await add_writer(self.http, s.gitea_url, token, org, app, s.merger_user)
        await become_reader(self.http, s.gitea_url, token, org, app, s.gitea_user)
        added = "given Write" if merger else "skipped (no such user yet)"
        return f"main protection {protected}, {s.merger_user} {added}, Shikigami down to Read"
