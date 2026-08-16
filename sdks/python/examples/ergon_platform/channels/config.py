from env import (
    CHANNELS_ATTACHMENT_DOWNLOAD_TIMEOUT,
    CHANNELS_BATCH_SIZE,
    CHANNELS_CLAIM_PAGE_SIZE,
    CHANNELS_CONFIG_ID,
    CHANNELS_INSTRUCTIONS_ADDRESS,
    CHANNELS_NACK_DELAY_SECONDS,
    CHANNELS_STREAMING,
    CHANNELS_VISIBILITY_TIMEOUT_SECONDS,
    ERGON_BASE_URL,
    ERGON_CLIENT_ID,
    ERGON_CLIENT_SECRET,
)
from ergon import task
from ergon.connector import ConnectorConfig
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.channels import (
    AsyncErgonPlatformChannelsConnector,
    ErgonPlatformChannelsConsumerConfig,
)
from policies import build_consumer_policy
from task import ChannelsEventTask

ERGON_PLATFORM_CLIENT = ErgonPlatformClient(
    client_id=ERGON_CLIENT_ID,
    client_secret=ERGON_CLIENT_SECRET,
    base_url=ERGON_BASE_URL,
)

CHANNELS_INSTRUCTIONS_CONSUMER_CONFIG = ErgonPlatformChannelsConsumerConfig(
    address=CHANNELS_INSTRUCTIONS_ADDRESS,
    config_id=CHANNELS_CONFIG_ID,
    batch_size=CHANNELS_BATCH_SIZE,
    visibility_timeout_seconds=CHANNELS_VISIBILITY_TIMEOUT_SECONDS,
    claim_page_size=CHANNELS_CLAIM_PAGE_SIZE,
    nack_delay_seconds=CHANNELS_NACK_DELAY_SECONDS,
    download_attachments=True,
    attachment_failure_policy="raise",
    attachment_download_timeout=CHANNELS_ATTACHMENT_DOWNLOAD_TIMEOUT,
)

CHANNELS_INSTRUCTIONS_CONNECTOR = ConnectorConfig(
    connector=AsyncErgonPlatformChannelsConnector,
    kwargs={
        "client": ERGON_PLATFORM_CLIENT,
        "consumer_config": CHANNELS_INSTRUCTIONS_CONSUMER_CONFIG,
    },
)

TASK_CHANNELS_EVENT_PROCESSOR = task.TaskConfig(
    name=ChannelsEventTask.name,
    task=ChannelsEventTask,
    max_workers=1,
    connectors={"consumer": CHANNELS_INSTRUCTIONS_CONNECTOR},
    policies=[build_consumer_policy(connector_name="consumer", streaming=CHANNELS_STREAMING)],
)

task.manager.register(TASK_CHANNELS_EVENT_PROCESSOR)

if __name__ == "__main__":
    import logging
    import sys

    from ergon.task.runner import run_task

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run_task(TASK_CHANNELS_EVENT_PROCESSOR, debug=True))
