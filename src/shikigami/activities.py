"""Temporal activities: the only places Shikigami touches the outside world. Each one may retry, so each is safe to
run twice."""

import secrets
from collections.abc import Awaitable, Callable

import httpx2
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami import build_creds as creds
from shikigami.config import Settings
from shikigami.models import AppAction, ExistingApp, RequestsSnapshot
from shikigami.openbao import KV
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
