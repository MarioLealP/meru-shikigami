"""`shikigami-bootstrap <app>`: runs OnboardBuild once with borrowed authority.

For Shikigami's own build credentials, which it can't make for itself (nothing would let it in yet). Run on the server
under an OpenBao admin session: a short-lived worker on its own queue, so it only ever picks up this run, which it
starts and waits for. The run is a normal Temporal workflow, with its history in the UI like any other.
"""

import argparse
import asyncio
import logging
import ssl
from pathlib import Path

import httpx2
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from shikigami.activities import BuildActivities
from shikigami.clients.openbao import KV
from shikigami.clients.temporal import connect
from shikigami.clients.tokens import TokenSource
from shikigami.config import Settings
from shikigami.workflows.workflows import OnboardBuild


class BootstrapSettings(BaseSettings):
    """What the admin session lends, from SHIKIGAMI_BOOTSTRAP_* variables (never arguments: those show in `ps`)."""

    model_config = SettingsConfigDict(env_prefix="SHIKIGAMI_BOOTSTRAP_", frozen=True)

    openbao_addr: str = "https://openbao.clockmost.internal:8200"
    openbao_ca: Path = Path("/etc/openbao/ca.pem")
    openbao_token: SecretStr  # the admin session's; revoked when the session ends
    runner_token: SecretStr  # minted by the admin session for this one app's repo
    task_queue: str = "shikigami-bootstrap"  # not Shikigami's: the real workers never see these tasks


async def run(app: str) -> str:
    """Run OnboardBuild for `app` on a worker of its own; returns the cosign public key."""
    settings, boot = Settings(), BootstrapSettings()
    tls = ssl.create_default_context(cafile=boot.openbao_ca)
    async with httpx2.AsyncClient(timeout=30) as http, httpx2.AsyncClient(timeout=30, verify=tls) as bao:
        tokens = TokenSource(
            http=http,
            token_url=settings.token_url,
            client_id=settings.temporal_client_id,
            token_file=settings.authentik_token_file,
        )
        client = await connect(settings, tokens)

        # KV and BuildActivities await their token callbacks, and a lent token has nothing to wait for.
        async def admin_token() -> str:  # ruff: ignore[unused-async] - the admin session's, fixed for the run
            return boot.openbao_token.get_secret_value()

        kv = KV(http=bao, addr=boot.openbao_addr, mount="apps", token=admin_token)

        async def runner_token(asked: str) -> str:  # ruff: ignore[unused-async] - as admin_token
            if asked != app:  # the lent token is scoped to one repo
                raise ApplicationError(f"the bootstrap only has a runner token for {app}", non_retryable=True)
            return boot.runner_token.get_secret_value()

        activities = BuildActivities(
            kv=kv, registry=settings.zot_registry, cosign=settings.cosign, runner_token=runner_token
        )
        async with Worker(
            client,
            task_queue=boot.task_queue,
            workflows=[OnboardBuild],
            activities=[
                activities.ensure_build_keys,
                activities.ensure_zot_login,
                activities.ensure_runner_registration,
            ],
        ):
            return await client.execute_workflow(
                OnboardBuild.run, app, id=f"onboard-build-{app}", task_queue=boot.task_queue
            )


def main() -> None:
    """The `shikigami-bootstrap` command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(asyncio.run(run(args.app)), end="")  # the cosign public key, for Meru/kyverno
