"""Temporal activities: the only places Shikigami touches the outside world. Each one may retry, so each is safe to
run twice."""

import secrets
from collections.abc import Awaitable, Callable, Mapping

import httpx2
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami import build_creds as creds
from shikigami import marker, retire, trust
from shikigami.argo import application, not_stopped
from shikigami.config import Settings
from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.openbao import KV
from shikigami.proposals import Edit, ProposalFailed, find_pr, open_pr
from shikigami.repos import (
    NoTemplate,
    add_writer,
    archive,
    become_reader,
    change,
    commit_files,
    delete_runners,
    dispatched_run,
    ensure_repo,
    head,
    is_admin,
    protect_main,
    public_file,
    read_file,
    rerun_failed,
    run_workflow,
)
from shikigami.sources import declare_apps, mark_retire_step, read_existing_apps, read_requests
from shikigami.tokens import TokenSource

PROPOSAL = """Shikigami's onboarding of `{app}` (D82): its {what}, derived - the request picks none of it.

{files}

Shikigami only reads this repo, so this is a proposal. It's merged by Xolotl once the request is approved on
Meru/requests and this PR only adds `{app}`; until Xolotl exists, by Mario. Closing it unmerged stops Shikigami
proposing it again."""

RETIREMENT = """Shikigami's retirement of `{app}` (D85): its {what} comes out - the app is stopped and its Argo CD
Application gone, so nothing uses it any more.

{files}

Shikigami only reads this repo, so this is a proposal. It's merged by Xolotl once the removal is approved on
Meru/requests and this PR only removes `{app}`'s entries; until Xolotl exists, by Mario. Closing it unmerged stops
Shikigami proposing it again."""

PROPOSALS = {"onboard": PROPOSAL, "retire": RETIREMENT}

STOP = """Shikigami's retirement of `{app}` (D85, cm-konk): the stop - `enabled: false` (no workload) and `build: false`
(no runner). The app's request is marked removed on Meru/requests.

Shikigami only reads this repo since onboarding (D89), so this is a proposal; Xolotl merges it once it matches the
rebuild from main and the request backs it. After Argo has applied it, the marker is parked in a second PR."""

PARK = """Shikigami's retirement of `{app}` (D85, cm-konk): the marker goes back to the template's
`deploy/meru.yaml.in`, so Meru stops deploying the repo. Onboarding again undoes it.

Opened only after Argo applied the stop. Xolotl merges it once it matches the rebuild from main."""


DEPLOY_PR = """Shikigami's onboarding of `{app}` (D81, D99): the marker that makes Meru deploy this repo -
`deploy/meru.yaml`, from the template's `deploy/meru.yaml.in` with the owner `{owner}` - and in `deploy/values.yaml`
the owner and the request's secrets as env vars ({secrets}), read by ESO from the app's own OpenBao path.

Opened once the app's fence and signing key are in place. Xolotl merges it once every file matches what it rebuilds
from main and the approved request; the first build starts after that."""


def trust_title(direction: str, app: str, what: str) -> str:
    """A trust PR's title, in the repos' Conventional Commits form - Xolotl reads it back to know what to rebuild."""
    return f"chore(trust): {direction} {app} - {what}"


def retire_title(step: str, app: str) -> str:
    """A retire PR's title in the app's own repo (cm-konk): `chore(retire): stop <app>` / `chore(retire): park <app>`
    - Xolotl reads it back to know what to rebuild."""
    return f"chore(retire): {step} {app}"


class Activities:
    def __init__(
        self,
        settings: Settings,
        http: httpx2.AsyncClient,
        thoth_tokens: TokenSource,
        kube: httpx2.AsyncClient | None = None,  # trusts the cluster's CA; only the retire pass reads the API
    ):
        self._settings, self._http, self._thoth_tokens, self._kube = settings, http, thoth_tokens, kube

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
    async def declare_apps(self, snapshot: RequestsSnapshot) -> dict:
        try:
            return await declare_apps(self._http, self._settings.thoth_url, await self._thoth_tokens.get(), snapshot)
        except httpx2.HTTPStatusError as e:
            if e.response.status_code in (403, 422):  # no permission, or Thoth refused the snapshot: retries won't help
                raise ApplicationError(
                    f"Thoth said {e.response.status_code}: {e.response.text[:500]}", non_retryable=True
                ) from e
            raise

    @activity.defn
    async def ensure_repo(self, app: str, template: str, description: str) -> bool:
        s = self._settings
        try:
            return await ensure_repo(
                self._http, s.gitea_url, s.gitea_token.get_secret_value(), s.apps_org, app, template, description
            )
        except NoTemplate as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        except httpx2.HTTPStatusError as e:
            if e.response.status_code in (403, 422):  # the bot may not create here / Gitea refused: retries won't help
                raise ApplicationError(
                    f"Gitea said {e.response.status_code}: {e.response.text[:500]}", non_retryable=True
                ) from e
            raise

    @activity.defn
    async def ensure_trust(self, app: str, description: str, public_key: str) -> str:
        """The app's fence (Meru/meru), its signing key + build exception (Meru/kyverno) and its push rule (Meru/zot),
        proposed as PRs (D82) - Shikigami only reads those repos. Each repo: kept if the app is already there, else one
        PR, never a second one while a PR with the same title exists (open, merged, or closed unmerged - a rejection
        isn't re-proposed). Another entry already there for the app is left for a person."""
        try:
            changes = [
                ("fence", "meru", "fence", await self._fence_files(app, description)),
                ("key", "kyverno", "signing key", await self._key_files(app, public_key)),
                ("push rule", "zot", "push rule", await self._push_rule_files(app)),
            ]
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        parts = []
        for label, repo, what, files in changes:
            if isinstance(files, trust.Conflict):
                parts.append(f"{label} waiting: {files} - by hand")
            else:
                parts.append(f"{label} {await self._propose(app, repo, what, files)}")
        return await self._ping_merger("; ".join(parts))

    async def _fence_files(self, app: str, description: str) -> dict[str, Edit]:
        org, path = self._settings.apps_org, f"projects/{app}.yaml"
        if await public_file(self._http, self._settings.gitea_url, org, "meru", path) is not None:
            return {}
        kustomization = await self._required(org, "meru", "kustomization.yaml")
        return {
            path: Edit(before=None, after=trust.fence(app, description)),
            "kustomization.yaml": Edit(
                before=kustomization, after=trust.with_resource(kustomization, "projects", path)
            ),
        }

    async def _key_files(self, app: str, public_key: str) -> dict[str, Edit] | trust.Conflict:
        org, exception = self._settings.apps_org, f"exceptions/{app}-build.yaml"
        current = await self._required(org, "kyverno", "keys/meru-signing-keys.yaml")
        try:
            updated = trust.with_key(current, app, public_key)
        except trust.Conflict as e:
            return e
        files = {"keys/meru-signing-keys.yaml": Edit(before=current, after=updated)} if updated != current else {}
        if await public_file(self._http, self._settings.gitea_url, org, "kyverno", exception) is None:
            kustomization = await self._required(org, "kyverno", "kustomization.yaml")
            files[exception] = Edit(before=None, after=trust.build_exception(app))
            files["kustomization.yaml"] = Edit(
                before=kustomization, after=trust.with_resource(kustomization, "exceptions", exception)
            )
        return files

    async def _push_rule_files(self, app: str) -> dict[str, Edit] | trust.Conflict:
        current = await self._required(self._settings.apps_org, "zot", "config.json")
        try:
            updated = trust.with_push_rule(current, app)
        except trust.Conflict as e:
            return e
        return {"config.json": Edit(before=current, after=updated)} if updated != current else {}

    async def _required(self, org: str, repo: str, path: str) -> str:
        text = await public_file(self._http, self._settings.gitea_url, org, repo, path)
        if text is None:
            raise trust.BadLayout(f"{org}/{repo} has no {path}")
        return text

    async def _ping_merger(self, result: str) -> str:
        """Tell Xolotl to sweep now if this pass opened a PR (D87). Best effort: its own sweep every 15 minutes is
        the backstop, so a failed ping is logged, never retried. Returns `result` unchanged."""
        if "proposed in" in result:
            try:
                (await self._http.post(self._settings.merger_ping_url, json={})).raise_for_status()
            except httpx2.HTTPError as e:
                activity.logger.warning("Xolotl not pinged: %s", e)
        return result

    async def _propose(
        self, app: str, repo: str, what: str, files: Mapping[str, Edit], direction: str = "onboard"
    ) -> str:
        if not files:
            return "kept"
        # the newest PR of either direction decides: after a retire and a restore, the old onboard PR doesn't count
        return await self._open_once(
            repo,
            trust_title(direction, app, what),
            [trust_title(d, app, what) for d in PROPOSALS],
            f"{direction}-{app}",
            PROPOSALS[direction].format(app=app, what=what, files="\n".join(f"- `{path}`" for path in files)),
            files,
        )

    async def _open_once(
        self, repo: str, title: str, titles: list[str], topic: str, description: str, files: Mapping[str, Edit]
    ) -> str:
        """One AGit PR with `title`, unless the newest PR among `titles` already is one: open -> waiting on it,
        merged -> not on main yet, closed unmerged -> a rejection, never proposed again. Each Edit's `before` must
        still be on main at the push (MainMoved otherwise: retried, so the change is worked out again)."""
        s = self._settings
        where = f"{s.apps_org}/{repo}"
        pr = await find_pr(self._http, s.gitea_url, s.apps_org, repo, titles)
        if pr is not None and pr.title == title:
            if pr.state == "open":
                return f"waiting on {where}#{pr.number}"
            if pr.merged:
                return f"{where}#{pr.number} merged, not on main yet"
            return f"{where}#{pr.number} was closed unmerged - not proposed again"
        try:
            await open_pr(
                f"{s.gitea_url}/{where}.git",
                s.gitea_user,
                s.gitea_token.get_secret_value(),
                topic,
                title,
                description,
                {path: edit.after for path, edit in files.items()},
                {path: edit.before for path, edit in files.items()},
            )
        except ProposalFailed as e:
            raise ApplicationError(str(e)) from e  # retried: a push can fail on the network too
        activity.logger.info("%s proposed in %s", title, where)
        return f"proposed in {where}"

    # The retire pass (D85), in this order, each step keeping what's done: stop -> wait for Argo -> park the marker ->
    # removal PRs -> revoke runner (+ the Zot login, BuildActivities) -> archive. Deletes nothing: Charon purges later.

    @activity.defn
    async def retire_stop(self, app: str) -> str:
        """`enabled: false` (no workload) + `build: false` (no runner): one commit while Shikigami still administers
        the repo, else a PR Xolotl merges (cm-konk) - "proposed in"/"waiting on" means the pass stops here until it's
        merged. Kept once both are there, or once the marker is parked (a later pass)."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            return await self._ping_merger(await self._stop_pr(app))
        marker_file = await read_file(self._http, s.gitea_url, token, org, app, "deploy/meru.yaml")
        if marker_file is None:
            return "kept (marker parked)"
        values = await read_file(self._http, s.gitea_url, token, org, app, "deploy/values.yaml")
        if values is None:
            raise ApplicationError(f"Meru/{app} has no deploy/values.yaml", non_retryable=True)
        if retire.is_stopped(values[0], marker_file[0]):
            return "kept"
        try:
            files = [
                change("update", "deploy/values.yaml", retire.stopped_values(values[0]), sha=values[1]),
                change("update", "deploy/meru.yaml", retire.stopped_marker(marker_file[0]), sha=marker_file[1]),
            ]
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        message = (
            f"chore(deploy): stop {app} - retired (D85)\n\n"
            "Removed by its request: no workload, no build runner. Shikigami."
        )
        await commit_files(self._http, s.gitea_url, token, org, app, message, files)
        return "written"

    async def _public(self, app: str, path: str) -> str | None:
        return await public_file(self._http, self._settings.gitea_url, self._settings.apps_org, app, path)

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
        except trust.BadLayout as e:
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
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        title, again = retire_title("park", app), [marker.TITLE.format(app=app)]
        return await self._open_once(app, title, [title, *again], f"retire-park-{app}", PARK.format(app=app), files)

    @activity.defn
    async def retire_wait_stopped(self, app: str) -> str:
        """Until Argo has synced the repo's current main with no workload left - retried by the workflow (30 s apart).
        No Application = already parked and gone."""
        s = self._settings
        if self._kube is None:
            raise ApplicationError("no Kubernetes API client (kubernetesApi off?)", non_retryable=True)
        found = await application(
            self._kube, s.kubernetes_url, s.kubernetes_dir / "token", s.argocd_namespace, f"app-{app}"
        )
        if found is None:
            return "gone"
        commit = await head(self._http, s.gitea_url, None, s.apps_org, app)  # public: Shikigami may have left (D89)
        reason = not_stopped(found, commit)
        if reason:
            raise ApplicationError(f"app-{app} not stopped yet: {reason}")
        return "stopped"

    @activity.defn
    async def retire_park_marker(self, app: str) -> str:
        """deploy/meru.yaml -> deploy/meru.yaml.in: the ApplicationSet drops the app. Only after retire_wait_stopped.
        One commit while Shikigami administers the repo, else a PR Xolotl merges (cm-konk)."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            return await self._ping_merger(await self._park_pr(app))
        marker_file = await read_file(self._http, s.gitea_url, token, org, app, "deploy/meru.yaml")
        if marker_file is None:
            return "kept"
        try:
            files = [
                change("create", "deploy/meru.yaml.in", retire.parked(marker_file[0])),
                change("delete", "deploy/meru.yaml", sha=marker_file[1]),
            ]
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        message = (
            f"chore(deploy): park {app}'s marker - retired (D85)\n\n"
            "Meru stops deploying the repo; onboarding again undoes it."
        )
        await commit_files(self._http, s.gitea_url, token, org, app, message, files)
        return "parked"

    @activity.defn
    async def retire_trust(self, app: str) -> str:
        """Removal PRs for the fence, the key + build exception and the push rule - only this app's entries."""
        org = self._settings.apps_org
        fence, exception = f"projects/{app}.yaml", f"exceptions/{app}-build.yaml"
        try:
            meru: dict[str, Edit] = {}
            if (fence_text := await public_file(self._http, self._settings.gitea_url, org, "meru", fence)) is not None:
                kustomization = await self._required(org, "meru", "kustomization.yaml")
                meru = {
                    fence: Edit(before=fence_text, after=None),
                    "kustomization.yaml": Edit(
                        before=kustomization, after=trust.without_resource(kustomization, fence)
                    ),
                }
            kyverno: dict[str, Edit] = {}
            keys = await self._required(org, "kyverno", "keys/meru-signing-keys.yaml")
            if (fewer := trust.without_key(keys, app)) != keys:
                kyverno["keys/meru-signing-keys.yaml"] = Edit(before=keys, after=fewer)
            exception_text = await public_file(self._http, self._settings.gitea_url, org, "kyverno", exception)
            if exception_text is not None:
                kustomization = await self._required(org, "kyverno", "kustomization.yaml")
                kyverno[exception] = Edit(before=exception_text, after=None)
                kyverno["kustomization.yaml"] = Edit(
                    before=kustomization, after=trust.without_resource(kustomization, exception)
                )
            config = await self._required(org, "zot", "config.json")
            zot: dict[str, Edit] = (
                {"config.json": Edit(before=config, after=rule)}
                if (rule := trust.without_push_rule(config, app)) != config
                else {}
            )
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        return await self._ping_merger(
            f"fence {await self._propose(app, 'meru', 'fence', meru, 'retire')}; "
            f"key {await self._propose(app, 'kyverno', 'signing key', kyverno, 'retire')}; "
            f"push rule {await self._propose(app, 'zot', 'push rule', zot, 'retire')}"
        )

    @activity.defn
    async def retire_runner(self, app: str) -> int:
        """Remove the repo's runners from Gitea (the runner pod is already gone with build: false). Needs repo admin:
        in a repo Shikigami left (D89) the offline record stays for Charon to clear at purge (cm-konk)."""
        s = self._settings
        token = s.gitea_token.get_secret_value()
        if not await is_admin(self._http, s.gitea_url, token, s.apps_org, app):
            activity.logger.info("%s: runner records left to Charon (Shikigami isn't admin)", app)
            return 0
        return await delete_runners(self._http, s.gitea_url, token, s.apps_org, app)

    @activity.defn
    async def retire_mark(self, app: str, status: str) -> str:
        """Thoth's side of the pass (D85): decommissioning at its start, retained at its end."""
        token = await self._thoth_tokens.get()
        return await mark_retire_step(self._http, self._settings.thoth_url, token, app, status)

    @activity.defn
    async def retire_archive(self, app: str) -> bool:
        """Archive the repo - last: nothing is written to it after this. Needs repo admin: a repo Shikigami left
        (D89) is archived by Charon at purge instead (cm-konk)."""
        s = self._settings
        token = s.gitea_token.get_secret_value()
        if not await is_admin(self._http, s.gitea_url, token, s.apps_org, app):
            activity.logger.info("%s: archiving left to Charon (Shikigami isn't admin)", app)
            return False
        return await archive(self._http, s.gitea_url, token, s.apps_org, app)

    @activity.defn
    async def ensure_deploy_files(self, app: str, owner: str, public_key: str, secrets: list[str] | None = None) -> str:
        """deploy/meru.yaml - the marker that makes Meru deploy the repo (D81) - from the template's
        deploy/meru.yaml.in with the request's owner, plus the owner and the request's secrets wired into
        deploy/values.yaml (D99), as one PR Xolotl rebuilds and merges (D99: no Shikigami write reaches a main
        unchecked). "kept" once the marker is on main: the repo is the app's then, never touched again. Waits (says
        why, the next sync tries again) until the app's fence and signing key are in place - a marker before them
        would deploy into nothing - and while the PR is open."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        # Read like Argo reads it (Meru/meru deploys public repos only), not with the bot's token: the restricted bot
        # can't see repos it didn't create, and those are exactly the ones that already have a marker.
        if await public_file(self._http, s.gitea_url, org, app, "deploy/meru.yaml") is not None:
            return "kept"
        if await public_file(self._http, s.gitea_url, org, "meru", f"projects/{app}.yaml") is None:
            return f"waiting: no fence Meru/meru projects/{app}.yaml"
        keys = await public_file(self._http, s.gitea_url, org, "kyverno", "keys/meru-signing-keys.yaml")
        if keys is None or not marker.key_trusted(keys, app, public_key):
            return f"waiting: Meru/kyverno doesn't trust {app}'s signing key yet"
        template = await read_file(self._http, s.gitea_url, token, org, app, "deploy/meru.yaml.in")
        if template is None:
            return "waiting: no deploy/meru.yaml.in (repo generated before the template had it)"
        values = await read_file(self._http, s.gitea_url, token, org, app, "deploy/values.yaml")
        try:
            files = {
                "deploy/meru.yaml": Edit(
                    before=None, after=marker.with_owner(template[0], owner, "deploy/meru.yaml.in")
                ),
                "deploy/meru.yaml.in": Edit(before=template[0], after=None),
            }
            if values is None:
                if secrets:
                    raise marker.BadTemplate("no deploy/values.yaml to wire the request's secrets into")
            else:
                text = retire.restarted_values(values[0])  # a restore (D106): the stop's line goes with the park
                if marker.owner_unset(text):
                    text = marker.with_owner(text, owner, "deploy/values.yaml")
                text = marker.with_secrets(text, secrets or [], "deploy/values.yaml")
                if text != values[0]:
                    files["deploy/values.yaml"] = Edit(before=values[0], after=text)
        except marker.BadTemplate as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        # Xolotl merges it: Write now, not only at the lock-out (D89). A repo Shikigami already left (a restore, D106)
        # kept Xolotl's Write from the first onboarding - and Shikigami couldn't give it anyway.
        if await is_admin(self._http, s.gitea_url, token, org, app) and not await add_writer(
            self._http, s.gitea_url, token, org, app, s.merger_user
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
        None yet -> started (D99: here, once the marker PR is merged). Passed -> done. Failed -> its failed jobs
        re-run, up to first_build_reruns times (Gitea counts the attempts, so nothing is remembered here). Still
        running, just started or just re-run -> an error, and the workflow's retry policy asks again a minute later.
        Not admin any more -> nothing to wait for."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            return "not watched (Shikigami already left the repo)"
        run = await dispatched_run(self._http, s.gitea_url, token, org, app)
        if run is None:
            # Asked for here, not with the marker: the marker is a PR now (D99), merged whenever Xolotl gets to it.
            # CI ignores deploy/**-only pushes, so the merge starts nothing by itself.
            await run_workflow(self._http, s.gitea_url, token, org, app, "ci.yaml")
            activity.logger.info("%s: first build started", app)
            raise ApplicationError("first build started")
        attempt = f"attempt {run.run_attempt}"
        if run.status != "completed":
            raise ApplicationError(f"first build {run.status} ({attempt})")
        if run.conclusion == "success":
            return f"passed ({attempt})"
        if run.run_attempt > s.first_build_reruns:
            return f"{run.conclusion} after {run.run_attempt} attempts - left to the owner"
        await rerun_failed(self._http, s.gitea_url, token, org, app, run.id)
        activity.logger.warning("%s: first build %s (%s), failed jobs re-run", app, run.conclusion, attempt)
        raise ApplicationError(f"first build {run.conclusion} ({attempt}), re-run")

    @activity.defn
    async def lock_out(self, app: str) -> str:
        """The last onboarding step (D89), once the marker is in: protect main (force push off), give the merger
        Write, and drop Shikigami to Read - it can still propose (retire PRs, cm-konk), never push. Kept once
        Shikigami isn't admin there any more - whoever made the repo or after it stepped down."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            return "kept"
        protected = await protect_main(self._http, s.gitea_url, token, org, app)
        merger = await add_writer(self._http, s.gitea_url, token, org, app, s.merger_user)
        await become_reader(self._http, s.gitea_url, token, org, app, s.gitea_user)
        added = "given Write" if merger else "skipped (no such user yet)"
        return f"main protection {protected}, {s.merger_user} {added}, Shikigami down to Read"

    @activity.defn
    async def dry_run(self, action: AppAction) -> None:
        """The steps not built yet: say what would happen. Each real step takes its place, one by one."""
        activity.logger.info("would %s app %s: %s", action.kind, action.app, "; ".join(action.reasons))


class BuildActivities:
    """An app's build credentials (D32, D66), each step safe to repeat: what exists is kept, what's missing is made,
    and every write is check-and-set (a conflict fails the attempt and Temporal retries it). Needs write rights in
    OpenBao - the first run (Shikigami's own) borrows an admin session's token."""

    def __init__(self, kv: KV, registry: str, cosign: str, runner_token: Callable[[str], Awaitable[str]]):
        self._kv, self._registry, self._cosign, self._runner_token = kv, registry, cosign, runner_token

    @activity.defn
    async def ensure_build_keys(self, app: str) -> str:
        """apps/meru/<app>-build/build: the Zot login + cosign key Sindri's build jobs read. Returns the public key
        (it goes into Meru/kyverno's trusted keys)."""
        path = f"meru/{app}-build/build"
        entry = await self._kv.read(path)
        if entry.data is None:
            key, password, public = await creds.new_cosign_key(self._cosign)
            login = creds.zot_config(self._registry, creds.builder(app), secrets.token_urlsafe(32))
            data = {
                "zot_config_b64": creds.b64(login),
                "cosign_key_b64": creds.b64(key),
                "cosign_password_b64": creds.b64(password),
            }
            await self._kv.write(path, data, cas=entry.version)
            activity.logger.info("made %s", path)
            return public
        if any(not entry.data.get(field) for field in creds.BUILD_FIELDS):
            # half an entry was written by hand; replacing it would break builds that use the other half
            raise ApplicationError(f"{path} is missing fields - fix it by hand", non_retryable=True)
        activity.logger.info("%s already there - kept", path)
        key, password = creds.unb64(entry.data["cosign_key_b64"]), creds.unb64(entry.data["cosign_password_b64"])
        return await creds.cosign_public_key(self._cosign, key, password)

    @activity.defn
    async def ensure_zot_login(self, app: str) -> bool:
        """The app's line in Zot's one htpasswd file (apps/meru/zot/eso/zot-htpasswd), matching the login in its
        build entry. Returns whether it changed."""
        build = await self._kv.read(f"meru/{app}-build/build")
        if build.data is None:
            raise ApplicationError(f"no build entry for {app} yet", non_retryable=True)
        user, password = creds.zot_login(creds.unb64(build.data["zot_config_b64"]), self._registry)
        path = "meru/zot/eso/zot-htpasswd"
        entry = await self._kv.read(path)
        if entry.data is None or "htpasswd" not in entry.data:
            raise ApplicationError(f"{path} has no htpasswd field", non_retryable=True)
        updated = creds.htpasswd_with(entry.data["htpasswd"], user, password)
        if updated is None:
            activity.logger.info("%s already in %s - kept", user, path)
            return False
        await self._kv.write(path, {**entry.data, "htpasswd": updated}, cas=entry.version)
        activity.logger.info("%s set in %s", user, path)
        return True

    @activity.defn
    async def ensure_runner_registration(self, app: str) -> bool:
        """apps/meru/<app>-build/eso/sindri-registration: the token the app's Sindri runner registers with, scoped
        to the app's repo. Returns whether it was made."""
        path = f"meru/{app}-build/eso/sindri-registration"
        entry = await self._kv.read(path)
        if entry.data and entry.data.get("token"):
            activity.logger.info("%s already there - kept", path)
            return False
        await self._kv.write(path, {"token": await self._runner_token(app)}, cas=entry.version)
        activity.logger.info("made %s", path)
        return True

    @activity.defn
    async def revoke_zot_login(self, app: str) -> bool:
        """The app's line out of Zot's htpasswd (retiring, D85) - an update, which Shikigami may do. Its build entry
        stays until Charon purges it. Returns whether it changed."""
        path = "meru/zot/eso/zot-htpasswd"
        entry = await self._kv.read(path)
        if entry.data is None or "htpasswd" not in entry.data:
            raise ApplicationError(f"{path} has no htpasswd field", non_retryable=True)
        updated = creds.htpasswd_without(entry.data["htpasswd"], creds.builder(app))
        if updated is None:
            return False
        await self._kv.write(path, {**entry.data, "htpasswd": updated}, cas=entry.version)
        activity.logger.info("%s removed from %s", creds.builder(app), path)
        return True
