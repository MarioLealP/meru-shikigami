"""`shikigami-run`: one process per pod - the Temporal worker and the HTTP side, side by side (D75). Replicas are
identical; Temporal hands each task to one of them."""

import asyncio
import logging
import ssl

import httpx2
import uvicorn
from temporalio.worker import Worker

from shikigami.activities import Activities, BuildActivities
from shikigami.config import Settings
from shikigami.openbao import KV, KubernetesLogin
from shikigami.repos import runner_token
from shikigami.temporal import connect, ensure_nightly, keep_token_fresh
from shikigami.tokens import TokenSource
from shikigami.web import make_app
from shikigami.workflows import AppWorkflow, FirstBuild, OnboardBuild, RequestsSync


async def run() -> None:
    settings = Settings()  # the secrets come from the environment
    tls = ssl.create_default_context(cafile=settings.openbao_dir / "ca.crt")
    cluster = ssl.create_default_context(cafile=settings.kubernetes_dir / "ca.crt")
    async with (
        httpx2.AsyncClient(timeout=30) as http,
        httpx2.AsyncClient(timeout=30, verify=tls) as bao,
        httpx2.AsyncClient(timeout=30, verify=cluster) as kube,
    ):

        def tokens(client_id: str) -> TokenSource:
            return TokenSource(http, settings.token_url, client_id, settings.authentik_token_file)

        temporal_tokens = tokens(settings.temporal_client_id)
        client = await connect(settings, temporal_tokens)
        await ensure_nightly(client, settings)
        activities = Activities(settings, http, tokens(settings.thoth_client_id), kube)
        login = KubernetesLogin(bao, settings.openbao_addr, settings.openbao_role, settings.openbao_dir / "token")
        kv = KV(bao, settings.openbao_addr, "apps", login.get)

        async def app_runner_token(app: str) -> str:
            gitea_token = settings.gitea_token.get_secret_value()
            return await runner_token(http, settings.gitea_url, gitea_token, settings.apps_org, app)

        build = BuildActivities(kv, settings.zot_registry, settings.cosign, app_runner_token)
        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[RequestsSync, AppWorkflow, OnboardBuild, FirstBuild],
            activities=[
                activities.read_requests,
                activities.read_existing_apps,
                activities.declare_apps,
                activities.ensure_repo,
                activities.ensure_trust,
                activities.ensure_deploy_files,
                activities.first_build,
                activities.lock_out,
                activities.retire_stop,
                activities.retire_wait_stopped,
                activities.retire_park_marker,
                activities.retire_trust,
                activities.retire_runner,
                activities.retire_archive,
                activities.retire_mark,
                activities.dry_run,
                build.ensure_build_keys,
                build.ensure_zot_login,
                build.ensure_runner_registration,
                build.revoke_zot_login,
            ],
            max_concurrent_activities=settings.max_concurrent_activities,
        )
        server = uvicorn.Server(
            uvicorn.Config(make_app(settings, client, http), host="0.0.0.0", port=settings.http_port)
        )
        async with asyncio.TaskGroup() as group:
            group.create_task(keep_token_fresh(client, temporal_tokens))
            group.create_task(worker.run())
            group.create_task(server.serve())


def main() -> None:
    # Temporal's worker, workflow and activity loggers (the dry run's "would ..." lines) log through the root logger.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run())
