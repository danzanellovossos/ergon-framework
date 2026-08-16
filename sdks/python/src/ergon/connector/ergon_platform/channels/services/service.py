from typing import Any, Optional

from ...models import ErgonPlatformClient
from .activity import ChannelsActivityService
from .addresses import ChannelsAddressService
from .attachments import ChannelsAttachmentService
from .messages import ChannelsMessageService


class ErgonPlatformChannelsService:
    """Domain-organized services over one shared Channels SDK client."""

    def __init__(
        self,
        config: ErgonPlatformClient,
        client: Any,
        download_client: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.client = client
        self._download_client = download_client
        self.activity = ChannelsActivityService(client)
        self.addresses = ChannelsAddressService(client)
        self.attachments = ChannelsAttachmentService(
            client,
            download_client=download_client,
        )
        self.messages = ChannelsMessageService(client)

    def close(self) -> None:
        """Close the services."""
        download = self._download_client
        if download is not None and download is not self.client and hasattr(download, "close"):
            download.close()
