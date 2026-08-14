from env import CHANNELS_BATCH_SIZE

from ergon.task import policies


def build_consumer_policy(
    connector_name: str = "consumer",
    streaming: bool = False,
) -> policies.ConsumerPolicy:
    """Return a ConsumerPolicy bound to the channels connector."""
    policy = policies.ConsumerPolicy()
    policy.name = "consumer"
    policy.fetch.connector_name = connector_name
    policy.fetch.batch.size = CHANNELS_BATCH_SIZE
    policy.loop.streaming = streaming
    policy.loop.limit = None if streaming else policy.fetch.batch.size

    poll_seconds = 5.0
    policy.fetch.empty.backoff = poll_seconds
    policy.fetch.empty.backoff_multiplier = 1.0
    policy.fetch.empty.backoff_cap = poll_seconds
    policy.transaction_runtime.timeout = 180.0

    return policy
