"""Consumer mode resolution shared by the sync and async workflow connectors.

A connector consumes one phase. Standard phases are served by the historical
list-and-claim loop; *Pull only* phases by ``POST /phases/{id}/items/pull``
under a consumer id. ``mode="auto"`` reads the phase kind once, on the first
call that needs it, and the connector then behaves like the matching kind for
the rest of its life. Phase kind is fixed at creation, so there is nothing to
re-check later.
"""

import logging
from typing import Any, Callable, Optional

from .._client import create_ergon_client, default_consumer_id
from ..models import ErgonPlatformClient
from ._operations import ErgonPlatformWorkflowsService
from .models import ErgonPlatformConsumerConfig

logger = logging.getLogger(__name__)

DEFAULT_NACK_RELEASE_DELAY_SECONDS = 10
DEFAULT_NACK_REASON = "nack_transaction"


class ConsumerModeState:
    """Lazily decides list-claim vs pull for one connector and swaps in the fenced client."""

    def __init__(
        self,
        client_config: ErgonPlatformClient,
        consumer_config: Optional[ErgonPlatformConsumerConfig],
        service: ErgonPlatformWorkflowsService,
        set_client: Callable[[Any], None],
    ) -> None:
        self._client_config = client_config
        self._consumer_config = consumer_config
        self._service = service
        self._set_client = set_client
        self._pull: Optional[bool] = None
        self.consumer_id: Optional[str] = None
        if consumer_config is not None and consumer_config.mode == "pull":
            self._enable_pull()
            self._pull = True

    @property
    def resolved(self) -> Optional[bool]:
        return self._pull

    def is_pull(self) -> bool:
        if self._pull is not None:
            return self._pull
        config = self._consumer_config
        if config is None:
            return False
        if config.mode == "list_claim":
            pull = False
        else:
            try:
                pull = self._service.is_pull_phase(config.workflow_id, config.phase_id)
            except Exception:
                logger.warning(
                    "Could not read the kind of phase %s; consuming it as a standard phase. "
                    "Set consumer_config.mode explicitly to silence this.",
                    config.phase_id,
                    exc_info=True,
                )
                pull = False
        if pull:
            self._enable_pull()
        self._pull = pull
        return pull

    def _enable_pull(self) -> None:
        config = self._consumer_config
        assert config is not None
        self.consumer_id = config.consumer_id or default_consumer_id()
        fenced = create_ergon_client(self._client_config, consumer_id=self.consumer_id)
        previous = self._service.client
        self._service.client = fenced
        self._set_client(fenced)
        if previous is not fenced:
            close = getattr(previous, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Closing the unfenced client failed", exc_info=True)
        logger.info("Phase %s is Pull only; consuming with consumer id %s", config.phase_id, self.consumer_id)
