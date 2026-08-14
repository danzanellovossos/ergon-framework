from env import (
    CHANNELS_ADDRESS,
    CHANNELS_BATCH_SIZE,
    CHANNELS_CONFIG_ID,
    CHANNELS_STREAMING,
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

CHANNELS_CONSUMER_CONFIG = ErgonPlatformChannelsConsumerConfig(
    address=CHANNELS_ADDRESS,
    config_id=CHANNELS_CONFIG_ID,
    batch_size=CHANNELS_BATCH_SIZE,
)

CHANNELS_CONSUMER_CONNECTOR = ConnectorConfig(
    connector=AsyncErgonPlatformChannelsConnector,
    kwargs={
        "client": ERGON_PLATFORM_CLIENT,
        "consumer_config": CHANNELS_CONSUMER_CONFIG,
    },
)

TASK_CHANNELS_EVENT_PROCESSOR = task.TaskConfig(
    name=ChannelsEventTask.name,
    task=ChannelsEventTask,
    max_workers=1,
    connectors={"consumer": CHANNELS_CONSUMER_CONNECTOR},
    policies=[build_consumer_policy(connector_name="consumer", streaming=CHANNELS_STREAMING)],
)

task.manager.register(TASK_CHANNELS_EVENT_PROCESSOR)


if __name__ == "__main__":
    import logging
    import sys

    from ergon.task.runner import run_task

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run_task(TASK_CHANNELS_EVENT_PROCESSOR, debug=True))
