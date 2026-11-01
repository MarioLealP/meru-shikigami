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
from shikigami.proposals import ProposalFailed, find_pr, open_pr
from shikigami.repos import (
    NoTemplate,
    add_writer,
    archive,
    change,
    commit_files,
    delete_runners,
    ensure_repo,
    head,
    is_admin,
    leave,
    protect_main,
    public_file,
    read_file,
    run_workflow,
)
from shikigami.sources import declare_apps, read_existing_apps, read_requests
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


def trust_title(direction: str, app: str, what: str) -> str:
    """A trust PR's title, in the repos' Conventional Commits form - Xolotl reads it back to know what to rebuild."""
    return f"chore(trust): {direction} {app} - {what}"


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
        return "; ".join(parts)

    async def _fence_files(self, app: str, description: str) -> dict[str, str]:
        org, path = self._settings.apps_org, f"projects/{app}.yaml"
        if await public_file(self._http, self._settings.gitea_url, org, "meru", path) is not None:
            return {}
        kustomization = await self._required(org, "meru", "kustomization.yaml")
        return {
            path: trust.fence(app, description),
            "kustomization.yaml": trust.with_resource(kustomization, "projects", path),
        }

    async def _key_files(self, app: str, public_key: str) -> dict[str, str] | trust.Conflict:
        org, exception = self._settings.apps_org, f"exceptions/{app}-build.yaml"
        current = await self._required(org, "kyverno", "keys/meru-signing-keys.yaml")
        try:
            updated = trust.with_key(current, app, public_key)
        except trust.Conflict as e:
            return e
        files = {"keys/meru-signing-keys.yaml": updated} if updated != current else {}
        if await public_file(self._http, self._settings.gitea_url, org, "kyverno", exception) is None:
            kustomization = await self._required(org, "kyverno", "kustomization.yaml")
            files[exception] = trust.build_exception(app)
            files["kustomization.yaml"] = trust.with_resource(kustomization, "exceptions", exception)
        return files

    async def _push_rule_files(self, app: str) -> dict[str, str] | trust.Conflict:
        current = await self._required(self._settings.apps_org, "zot", "config.json")
        try:
            updated = trust.with_push_rule(current, app)
        except trust.Conflict as e:
            return e
        return {"config.json": updated} if updated != current else {}

    async def _required(self, org: str, repo: str, path: str) -> str:
        text = await public_file(self._http, self._settings.gitea_url, org, repo, path)
        if text is None:
            raise trust.BadLayout(f"{org}/{repo} has no {path}")
        return text

    async def _propose(
        self, app: str, repo: str, what: str, files: Mapping[str, str | None], direction: str = "onboard"
    ) -> str:
        if not files:
            return "kept"
        s = self._settings
        where, title = f"{s.apps_org}/{repo}", trust_title(direction, app, what)
        # the newest PR of either direction decides: after a retire and a restore, the old onboard PR doesn't count
        titles = [trust_title(d, app, what) for d in PROPOSALS]
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
                f"{direction}-{app}",
                title,
                PROPOSALS[direction].format(app=app, what=what, files="\n".join(f"- `{path}`" for path in files)),
                files,
            )
        except ProposalFailed as e:
            raise ApplicationError(str(e)) from e  # retried: a push can fail on the network too
        activity.logger.info("%s: %s %s proposed in %s", app, direction, what, where)
        return f"proposed in {where}"

    # The retire pass (D85), in this order, each step keeping what's done: stop -> wait for Argo -> park the marker ->
    # removal PRs -> revoke runner (+ the Zot login, BuildActivities) -> archive. Deletes nothing: Charon purges later.

    @activity.defn
    async def retire_stop(self, app: str) -> str:
        """One commit: `enabled: false` (no workload) + `build: false` (no runner). Kept once both are there, or once
        the marker is parked (a later pass). A repo Shikigami left at onboarding (D89) is refused: there its reads
        come back empty, which would pass for "parked" - retiring those needs the stop + park as PRs (next step)."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            raise ApplicationError(
                f"Shikigami left {org}/{app} at onboarding (D89): retiring it needs the stop + park as PRs (not built)",
                non_retryable=True,
            )
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
        commit = await head(self._http, s.gitea_url, s.gitea_token.get_secret_value(), s.apps_org, app)
        reason = not_stopped(found, commit)
        if reason:
            raise ApplicationError(f"app-{app} not stopped yet: {reason}")
        return "stopped"

    @activity.defn
    async def retire_park_marker(self, app: str) -> str:
        """deploy/meru.yaml -> deploy/meru.yaml.in (one commit): the ApplicationSet drops the app. Only after
        retire_wait_stopped."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
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
            meru: dict[str, str | None] = {}
            if await public_file(self._http, self._settings.gitea_url, org, "meru", fence) is not None:
                kustomization = await self._required(org, "meru", "kustomization.yaml")
                meru = {fence: None, "kustomization.yaml": trust.without_resource(kustomization, fence)}
            kyverno: dict[str, str | None] = {}
            keys = await self._required(org, "kyverno", "keys/meru-signing-keys.yaml")
            if (fewer := trust.without_key(keys, app)) != keys:
                kyverno["keys/meru-signing-keys.yaml"] = fewer
            if await public_file(self._http, self._settings.gitea_url, org, "kyverno", exception) is not None:
                kustomization = await self._required(org, "kyverno", "kustomization.yaml")
                kyverno[exception] = None
                kyverno["kustomization.yaml"] = trust.without_resource(kustomization, exception)
            config = await self._required(org, "zot", "config.json")
            zot: dict[str, str | None] = (
                {"config.json": rule} if (rule := trust.without_push_rule(config, app)) != config else {}
            )
        except trust.BadLayout as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        return (
            f"fence {await self._propose(app, 'meru', 'fence', meru, 'retire')}; "
            f"key {await self._propose(app, 'kyverno', 'signing key', kyverno, 'retire')}; "
            f"push rule {await self._propose(app, 'zot', 'push rule', zot, 'retire')}"
        )

    @activity.defn
    async def retire_runner(self, app: str) -> int:
        """Remove the repo's runners from Gitea (the runner pod is already gone with build: false)."""
        s = self._settings
        return await delete_runners(self._http, s.gitea_url, s.gitea_token.get_secret_value(), s.apps_org, app)

    @activity.defn
    async def retire_archive(self, app: str) -> bool:
        """Archive the repo - last: nothing is written to it after this."""
        s = self._settings
        return await archive(self._http, s.gitea_url, s.gitea_token.get_secret_value(), s.apps_org, app)

    @activity.defn
    async def ensure_deploy_files(self, app: str, owner: str, public_key: str) -> str:
        """deploy/meru.yaml - the marker that makes Meru deploy the repo (D81) - from the template's
        deploy/meru.yaml.in with the request's owner, pushed straight to main in one commit with the owner in
        deploy/values.yaml. Only while the marker is missing: once it exists the repo is the app's, never touched again.
        Waits (says why, next sync tries again) until the app's fence and its signing key are in place - a marker
        before them would deploy into nothing."""
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
            files = [
                change("create", "deploy/meru.yaml", marker.with_owner(template[0], owner, "deploy/meru.yaml.in")),
                change("delete", "deploy/meru.yaml.in", sha=template[1]),
            ]
            if values and marker.owner_unset(values[0]):
                filled = marker.with_owner(values[0], owner, "deploy/values.yaml")
                files.append(change("update", "deploy/values.yaml", filled, sha=values[1]))
        except marker.BadTemplate as e:
            raise ApplicationError(str(e), non_retryable=True) from e
        message = (
            f"chore(deploy): {app} is a Meru app (owner {owner})\n\n"
            "Written by Shikigami (D81): fence, signing key and build credentials are in place."
        )
        await commit_files(self._http, s.gitea_url, token, org, app, message, files)
        activity.logger.info("%s: deploy/meru.yaml written", app)
        # The first build: CI ignores deploy/**-only pushes, so the marker commit starts nothing by itself. A retry
        # after this point finds the marker and stops at "kept" - the build is asked for once; if this call fails,
        # the app waits on its placeholder digest until someone presses "Run workflow" (the failure is in the history).
        await run_workflow(self._http, s.gitea_url, token, org, app, "ci.yaml")
        activity.logger.info("%s: first build started", app)
        return "written, first build started"

    @activity.defn
    async def lock_out(self, app: str) -> str:
        """The last onboarding step (D89), once the marker is in: protect main (force push off), give the merger
        Write, and leave the repo. Kept once Shikigami isn't admin there any more - whoever made the repo (Mario's own
        repos never had Shikigami) or after it left."""
        s = self._settings
        token, org = s.gitea_token.get_secret_value(), s.apps_org
        if not await is_admin(self._http, s.gitea_url, token, org, app):
            return "kept"
        protected = await protect_main(self._http, s.gitea_url, token, org, app)
        merger = await add_writer(self._http, s.gitea_url, token, org, app, s.merger_user)
        await leave(self._http, s.gitea_url, token, org, app, s.gitea_user)
        added = "given Write" if merger else "skipped (no such user yet)"
        return f"main protection {protected}, {s.merger_user} {added}, Shikigami left"

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
