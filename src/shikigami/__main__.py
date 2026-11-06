"""`shikigami-run`: one process per pod - the Temporal worker and the HTTP side, side by side (D75).

Replicas are identical; Temporal hands each task to one of them.
"""

import asyncio
import logging
import ssl

import httpx2
import sentry_sdk
import uvicorn
from temporalio.worker import Worker

from shikigami.activities import Activities, BuildActivities
from shikigami.api.app import make_app
from shikigami.clients.openbao import KV, KubernetesLogin
from shikigami.clients.repos import runner_token
from shikigami.clients.temporal import connect, ensure_nightly, keep_token_fresh
from shikigami.clients.tokens import TokenSource
from shikigami.config import Settings
from shikigami.workflows.workflows import AppWorkflow, FirstBuild, OnboardBuild, RequestsSync


async def run() -> None:
    """Connect everything, then run the Temporal worker and the HTTP server until one of them stops."""
    settings = Settings()  # the secrets come from the environment
    tls = ssl.create_default_context(cafile=settings.openbao_dir / "ca.crt")
    cluster = ssl.create_default_context(cafile=settings.kubernetes_dir / "ca.crt")
    async with (
        httpx2.AsyncClient(timeout=30) as http,
        httpx2.AsyncClient(timeout=30, verify=tls) as bao,
        httpx2.AsyncClient(timeout=30, verify=cluster) as kube,
    ):

        def tokens(client_id: str) -> TokenSource:
            return TokenSource(
                http=http,
                token_url=settings.token_url,
                client_id=client_id,
                token_file=settings.authentik_token_file,
            )

        temporal_tokens = tokens(settings.temporal_client_id)
        client = await connect(settings, temporal_tokens)
        await ensure_nightly(client, settings)
        activities = Activities(settings=settings, http=http, thoth_tokens=tokens(settings.thoth_client_id), kube=kube)
        login = KubernetesLogin(
            http=bao, addr=settings.openbao_addr, role=settings.openbao_role, jwt_file=settings.openbao_dir / "token"
        )
        kv = KV(http=bao, addr=settings.openbao_addr, mount="apps", token=login.get)

        async def app_runner_token(app: str) -> str:
            gitea_token = settings.gitea_token.get_secret_value()
            return await runner_token(http, settings.gitea_url, gitea_token, settings.apps_org, app)

        build = BuildActivities(
            kv=kv, registry=settings.zot_registry, cosign=settings.cosign, runner_token=app_runner_token
        )
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
                activities.ensure_glitchtip,
                activities.ensure_deploy_files,
                activities.first_build,
                activities.lock_out,
                activities.retire_stop,
                activities.retire_wait_stopped,
                activities.retire_park_marker,
                activities.retire_trust,
                activities.retire_runner,
                activities.retire_archive,
                activities.retire_mark_namespaces,
                activities.restore_unmark_namespaces,
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
    """The `shikigami-run` command."""
    # Crash reports to GlitchTip (D125): SENTRY_DSN comes from nuwa-app's glitchtip.dsn; unset, the SDK stays off.
    # No local variables or request bodies in a report - either can hold a secret.
    sentry_sdk.init(include_local_variables=False, max_request_body_size="never")
    # Temporal's worker, workflow and activity loggers (the dry run's "would ..." lines) log through the root logger.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
