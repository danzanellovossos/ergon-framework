"""Configure and run the Outlook connector as an Ergon consumer task."""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from task import OutlookEmailTask

from ergon import task as ergon_task
from ergon.connector import ConnectorConfig
from ergon.connector.outlook import (
    AsyncOutlookGraphConnector,
    OutlookAckActionConfig,
    OutlookConsumerConfig,
    OutlookGraphClient,
    OutlookMessageFilter,
    OutlookNackActionConfig,
)
from ergon.task import policies

load_dotenv(Path(__file__).parent / ".env")

OUTLOOK_BATCH_SIZE = 50
OUTLOOK_STREAMING = False
OUTLOOK_ATTACHMENT_DIR = Path(os.getenv("OUTLOOK_ATTACHMENT_DIR") or Path(__file__).parent / "downloads")

OutlookEmailTask.attachment_dir = OUTLOOK_ATTACHMENT_DIR

OUTLOOK_CLIENT = OutlookGraphClient(
    tenant_id=os.environ["OUTLOOK_TENANT_ID"],
    client_id=os.environ["OUTLOOK_CLIENT_ID"],
    client_secret=os.environ["OUTLOOK_CLIENT_SECRET"],
    user_email=os.environ["OUTLOOK_USER_EMAIL"],
)

OUTLOOK_CONSUMER_CONFIG = OutlookConsumerConfig(
    folder_id=os.getenv("OUTLOOK_FOLDER_ID", "Inbox"),
    filter=OutlookMessageFilter(unread_only=True, has_attachments=True),
    batch_size=OUTLOOK_BATCH_SIZE,
    download_attachments=True,
    ack_config=OutlookAckActionConfig(mark_as_read=True, move_to_folder_id="deleteditems"),
    nack_config=OutlookNackActionConfig(categories=["Ergon processing failed"]),
)

OUTLOOK_CONNECTOR = ConnectorConfig(
    connector=AsyncOutlookGraphConnector,
    kwargs={
        "client": OUTLOOK_CLIENT,
        "consumer_config": OUTLOOK_CONSUMER_CONFIG,
    },
)

OUTLOOK_CONSUMER_POLICY = policies.ConsumerPolicy()
OUTLOOK_CONSUMER_POLICY.name = "consumer"
OUTLOOK_CONSUMER_POLICY.fetch.connector_name = "consumer"
OUTLOOK_CONSUMER_POLICY.fetch.batch.size = OUTLOOK_BATCH_SIZE
OUTLOOK_CONSUMER_POLICY.loop.streaming = OUTLOOK_STREAMING
OUTLOOK_CONSUMER_POLICY.loop.limit = None if OUTLOOK_STREAMING else OUTLOOK_BATCH_SIZE
OUTLOOK_CONSUMER_POLICY.fetch.empty.backoff = 60
OUTLOOK_CONSUMER_POLICY.fetch.empty.backoff_multiplier = 1.0
OUTLOOK_CONSUMER_POLICY.fetch.empty.backoff_cap = 60

TASK_OUTLOOK_EMAIL_PROCESSOR = ergon_task.TaskConfig(
    name=OutlookEmailTask.name,
    task=OutlookEmailTask,
    max_workers=1,
    connectors={"consumer": OUTLOOK_CONNECTOR},
    policies=[OUTLOOK_CONSUMER_POLICY],
)

ergon_task.manager.register(TASK_OUTLOOK_EMAIL_PROCESSOR)


if __name__ == "__main__":
    from ergon.task.runner import run_task

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run_task(TASK_OUTLOOK_EMAIL_PROCESSOR, debug=True))
