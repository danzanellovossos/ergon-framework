"""Sync streaming consumer used by test_shutdown_e2e.py as a real process.

Every lifecycle step is appended as one JSON line to ``$SHUTDOWN_APP_EVENTS`` so the
test can check, per worker pid, what was fetched, started, finished and released.
"""

import json
import multiprocessing
import os
import sys
import time

from ergon.connector import Connector, ConnectorConfig, Transaction
from ergon.task import policies, runner
from ergon.task.base import TaskConfig
from ergon.task.mixins import ConsumerTask

QUEUE_SIZE = int(os.environ.get("SHUTDOWN_APP_QUEUE", "20"))
BATCH_SIZE = int(os.environ.get("SHUTDOWN_APP_BATCH", "3"))
PROCESS_SECONDS = float(os.environ.get("SHUTDOWN_APP_PROCESS_SECONDS", "1"))
WORKERS = int(os.environ.get("SHUTDOWN_APP_WORKERS", "1"))


def record(event: str, **fields) -> None:
    line = json.dumps({"event": event, "pid": os.getpid(), **fields})
    with open(os.environ["SHUTDOWN_APP_EVENTS"], "a") as fh:
        fh.write(line + "\n")


class QueueConnector(Connector):
    def __init__(self):
        self._queue = [Transaction(id=f"{os.getpid()}-{i}", payload={}) for i in range(QUEUE_SIZE)]

    def fetch_transactions(self, batch_size: int = 1, **kwargs):
        batch, self._queue = self._queue[:batch_size], self._queue[batch_size:]
        record("fetched", ids=[t.id for t in batch])
        return batch

    def dispatch_transactions(self, transactions, **kwargs):
        return None

    def release_transaction(self, transaction: Transaction) -> None:
        record("released", id=transaction.id)


class SlowConsumer(ConsumerTask):
    def process_transaction(self, transaction):
        record("started", id=transaction.id)
        time.sleep(PROCESS_SECONDS)
        record("finished", id=transaction.id)

    def execute(self):
        return self.consume_transactions(
            policies.ConsumerPolicy(
                loop=policies.ConsumerLoopPolicy(streaming=True),
                fetch=policies.FetchPolicy(
                    batch=policies.BatchPolicy(size=BATCH_SIZE, max_size=BATCH_SIZE),
                    empty=policies.EmptyFetchPolicy(backoff=60),
                ),
                transaction_runtime=policies.TransactionRuntimePolicy(timeout=PROCESS_SECONDS + 60),
            )
        )


def main() -> None:
    start_method = os.environ.get("SHUTDOWN_APP_START_METHOD")
    if start_method:
        multiprocessing.set_start_method(start_method, force=True)
    config = TaskConfig(
        name="shutdown-e2e",
        task=SlowConsumer,
        connectors={"queue": ConnectorConfig(connector=QueueConnector)},
        max_workers=WORKERS,
    )
    sys.exit(runner.run_task(config))
