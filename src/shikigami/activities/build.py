"""An app's build credentials (D32, D66): the activities that write them to OpenBao."""

import secrets
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami import build_creds as creds
from shikigami.openbao import KV


class BuildActivities(BaseModel):
    """An app's build credentials (D32, D66), each step safe to repeat.

    What exists is kept, what's missing is made, and every write is check-and-set (a conflict fails the attempt and
    Temporal retries it). Needs write rights in OpenBao - the first run (Shikigami's own) borrows an admin session's
    token.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kv: KV
    registry: str
    cosign: str
    runner_token: Callable[[str], Awaitable[str]]

    @activity.defn
    async def ensure_build_keys(self, app: str) -> str:
        """apps/meru/<app>-build/build: the Zot login + cosign key Sindri's build jobs read.

        Returns the public key (it goes into Meru/kyverno's trusted keys).
        """
        path = f"meru/{app}-build/build"
        entry = await self.kv.read(path)
        if entry.data is None:
            key, password, public = await creds.new_cosign_key(self.cosign)
            login = creds.zot_config(self.registry, creds.builder(app), secrets.token_urlsafe(32))
            data = {
                "zot_config_b64": creds.b64(login),
                "cosign_key_b64": creds.b64(key),
                "cosign_password_b64": creds.b64(password),
            }
            await self.kv.write(path, data, cas=entry.version)
            activity.logger.info("made %s", path)
            return public
        if any(not entry.data.get(field) for field in creds.BUILD_FIELDS):
            # half an entry was written by hand; replacing it would break builds that use the other half
            raise ApplicationError(f"{path} is missing fields - fix it by hand", non_retryable=True)
        activity.logger.info("%s already there - kept", path)
        key, password = creds.unb64(entry.data["cosign_key_b64"]), creds.unb64(entry.data["cosign_password_b64"])
        return await creds.cosign_public_key(self.cosign, key, password)

    @activity.defn
    async def ensure_zot_login(self, app: str) -> bool:
        """Put the app's line in Zot's one htpasswd file, matching the login in its build entry.

        The file is apps/meru/zot/eso/zot-htpasswd. Returns whether it changed.
        """
        build = await self.kv.read(f"meru/{app}-build/build")
        if build.data is None:
            raise ApplicationError(f"no build entry for {app} yet", non_retryable=True)
        user, password = creds.zot_login(creds.unb64(build.data["zot_config_b64"]), self.registry)
        path = "meru/zot/eso/zot-htpasswd"
        entry = await self.kv.read(path)
        if entry.data is None or "htpasswd" not in entry.data:
            raise ApplicationError(f"{path} has no htpasswd field", non_retryable=True)
        updated = creds.htpasswd_with(entry.data["htpasswd"], user, password)
        if updated is None:
            activity.logger.info("%s already in %s - kept", user, path)
            return False
        await self.kv.write(path, {**entry.data, "htpasswd": updated}, cas=entry.version)
        activity.logger.info("%s set in %s", user, path)
        return True

    @activity.defn
    async def ensure_runner_registration(self, app: str) -> bool:
        """Make the token the app's Sindri runner registers with, scoped to the app's repo.

        Its path is apps/meru/<app>-build/eso/sindri-registration. Returns whether it was made.
        """
        path = f"meru/{app}-build/eso/sindri-registration"
        entry = await self.kv.read(path)
        if entry.data and entry.data.get("token"):
            activity.logger.info("%s already there - kept", path)
            return False
        await self.kv.write(path, {"token": await self.runner_token(app)}, cas=entry.version)
        activity.logger.info("made %s", path)
        return True

    @activity.defn
    async def revoke_zot_login(self, app: str) -> bool:
        """The app's line out of Zot's htpasswd (retiring, D85) - an update, which Shikigami may do.

        Its build entry stays until Charon purges it. Returns whether it changed.
        """
        path = "meru/zot/eso/zot-htpasswd"
        entry = await self.kv.read(path)
        if entry.data is None or "htpasswd" not in entry.data:
            raise ApplicationError(f"{path} has no htpasswd field", non_retryable=True)
        updated = creds.htpasswd_without(entry.data["htpasswd"], creds.builder(app))
        if updated is None:
            return False
        await self.kv.write(path, {**entry.data, "htpasswd": updated}, cas=entry.version)
        activity.logger.info("%s removed from %s", creds.builder(app), path)
        return True
