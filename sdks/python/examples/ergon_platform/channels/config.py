from env import (
    CHANNELS_ATTACHMENT_DOWNLOAD_TIMEOUT,
    CHANNELS_AUTH_CODE_ADDRESS,
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
from policies import build_consumer_policy
from task import ChannelsEventTask

from ergon import task
from ergon.connector import ConnectorConfig
from ergon.connector.ergon_platform import ErgonPlatformClient
from ergon.connector.ergon_platform.channels import (
    AsyncErgonPlatformChannelsConnector,
    ChannelsActivityFilter,
    ErgonPlatformChannelsConsumerConfig,
)

ERGON_PLATFORM_CLIENT = ErgonPlatformClient(
    client_id=ERGON_CLIENT_ID,
    client_secret=ERGON_CLIENT_SECRET,
    base_url=ERGON_BASE_URL,
)

# ``subscription_id`` is intentionally omitted. The connector derives a stable
# UUID from config + address + filter, so these two consumers remain independent.
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

CHANNELS_AUTH_CODE_CONSUMER_CONFIG = ErgonPlatformChannelsConsumerConfig(
    address=CHANNELS_AUTH_CODE_ADDRESS,
    config_id=CHANNELS_CONFIG_ID,
    batch_size=CHANNELS_BATCH_SIZE,
    visibility_timeout_seconds=CHANNELS_VISIBILITY_TIMEOUT_SECONDS,
    claim_page_size=CHANNELS_CLAIM_PAGE_SIZE,
    nack_delay_seconds=CHANNELS_NACK_DELAY_SECONDS,
    download_attachments=False,
    activity_filter=ChannelsActivityFilter(
        received_only=True,
        from_address="atendimentocbt@jsl.com.br",
        subject_contains="codigo de acesso",
    ),
)

CHANNELS_INSTRUCTIONS_CONNECTOR = ConnectorConfig(
    connector=AsyncErgonPlatformChannelsConnector,
    kwargs={
        "client": ERGON_PLATFORM_CLIENT,
        "consumer_config": CHANNELS_INSTRUCTIONS_CONSUMER_CONFIG,
    },
)

CHANNELS_AUTH_CODE_CONNECTOR = ConnectorConfig(
    connector=AsyncErgonPlatformChannelsConnector,
    kwargs={
        "client": ERGON_PLATFORM_CLIENT,
        "consumer_config": CHANNELS_AUTH_CODE_CONSUMER_CONFIG,
    },
)

TASK_CHANNELS_EVENT_PROCESSOR = task.TaskConfig(
    name=ChannelsEventTask.name,
    task=ChannelsEventTask,
    max_workers=1,
    connectors={
        "consumer": CHANNELS_INSTRUCTIONS_CONNECTOR,
        "auth_code": CHANNELS_AUTH_CODE_CONNECTOR,
    },
    policies=[build_consumer_policy(connector_name="consumer", streaming=CHANNELS_STREAMING)],
)

task.manager.register(TASK_CHANNELS_EVENT_PROCESSOR)


if __name__ == "__main__":
    import logging
    import sys

    from ergon.task.runner import run_task

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run_task(TASK_CHANNELS_EVENT_PROCESSOR, debug=True))
