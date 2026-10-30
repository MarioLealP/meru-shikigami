"""Shikigami's Temporal connection: mTLS + an Authentik token that's swapped before it expires (D70)."""

import asyncio
import contextlib

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import TLSConfig

from shikigami.config import Settings
from shikigami.tokens import TokenSource
from shikigami.workflows import SYNC_ID, RequestsSync

REFRESH_EVERY = 240  # seconds; tokens last 10 min, TokenSource renews one a minute before it runs out
NIGHTLY_ID = "requests-sync-nightly"


async def connect(settings: Settings, tokens: TokenSource) -> Client:
    tls = settings.tls_dir
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=await tokens.get(),  # sent as "authorization: Bearer <token>"
        tls=TLSConfig(
            server_root_ca_cert=(tls / "ca.crt").read_bytes(),
            client_cert=(tls / "tls.crt").read_bytes(),
            client_private_key=(tls / "tls.key").read_bytes(),
            domain=settings.temporal_server_name,
        ),
        data_converter=pydantic_data_converter,
    )


async def keep_token_fresh(client: Client, tokens: TokenSource) -> None:
    while True:
        await asyncio.sleep(REFRESH_EVERY)
        client.api_key = await tokens.get()


async def ensure_nightly(client: Client, settings: Settings) -> None:
    """The nightly re-read (D65). Every replica tries at start; the first one creates it, the rest find it."""
    with contextlib.suppress(ScheduleAlreadyRunningError):
        await client.create_schedule(
            NIGHTLY_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(RequestsSync.run, id=SYNC_ID, task_queue=settings.task_queue),
                spec=ScheduleSpec(cron_expressions=[settings.nightly_cron]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )


async def backlog(client: Client, settings: Settings) -> int:
    """Tasks waiting for a worker on Shikigami's queue (workflow + activity tasks) - what KEDA scales on (D75)."""
    total = 0
    for queue_type in (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY):
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=settings.temporal_namespace,
                task_queue=TaskQueue(name=settings.task_queue),
                task_queue_type=queue_type,
                report_stats=True,
            )
        )
        total += response.stats.approximate_backlog_count
    return total
