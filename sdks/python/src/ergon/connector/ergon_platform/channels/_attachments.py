import logging
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Union

from ...transaction import Transaction
from ._activity import ActivityAdapter
from .models import InboxAttachmentFile

logger = logging.getLogger(__name__)


class InboxAttachments:
    """Fetches attachment bytes for a hydrated inbox ``Transaction``.

    ``download_client`` should be a dedicated SDK client (short timeout, no
    retries) so a stuck CDN download cannot block ack/nack on the main client.
    """

    def __init__(self, client: Any, *, download_client: Optional[Any] = None) -> None:
        self._client = download_client or client

    def download_bytes(self, config_id: str, event_id: str, attachment_id: str) -> bytes:
        """Download an attachment bytes from the platform."""
        return self._client.channels.configs.activity_attachment_file(config_id, event_id, attachment_id)

    def hydrate(self, config_id: str, transaction: Transaction) -> Transaction:
        """Hydrate a transaction with attachment bytes.

        A failed or timed-out file is skipped (metadata kept, no ``content``)
        so one slow CDN object does not fail the whole event.
        """
        event_id = transaction.id
        if not event_id:
            return transaction

        enriched: List[Dict[str, Any]] = []
        changed = False
        for meta in ActivityAdapter.attachments(transaction):
            if meta.get("content") is not None:
                enriched.append(meta)
                continue
            attachment_id = ActivityAdapter.attachment_id(meta)
            if not attachment_id:
                enriched.append(meta)
                continue
            filename = str(meta.get("filename") or attachment_id)
            logger.info("Downloading attachment %s (%s)", filename, attachment_id)
            try:
                content = self.download_bytes(config_id, event_id, attachment_id)
            except Exception:
                logger.warning(
                    "Skipping attachment %s (%s) for event %s",
                    filename,
                    attachment_id,
                    event_id,
                    exc_info=True,
                )
                enriched.append(meta)
                continue
            enriched.append({**meta, "content": content})
            changed = True
        if not changed:
            return transaction
        return ActivityAdapter.with_attachments(transaction, enriched)

    def download_all(
        self,
        config_id: str,
        transaction: Transaction,
        dest: Optional[Union[str, Path]] = None,
    ) -> List[InboxAttachmentFile]:
        """Download all attachments from a transaction."""
        event_id = transaction.id
        if not event_id:
            raise ValueError("transaction.id is required to download attachments")

        dest_dir = Path(dest) / str(event_id) if dest is not None else None
        downloaded: List[InboxAttachmentFile] = []
        for meta in ActivityAdapter.attachments(transaction):
            attachment_id = ActivityAdapter.attachment_id(meta)
            if not attachment_id:
                continue
            filename = PurePosixPath(str(meta.get("filename") or attachment_id)).name or attachment_id
            logger.info("Downloading attachment %s (%s)", filename, attachment_id)
            content = self.download_bytes(config_id, event_id, attachment_id)
            path: Optional[str] = None
            if dest_dir is not None:
                dest_dir.mkdir(parents=True, exist_ok=True)
                file_path = dest_dir / filename
                file_path.write_bytes(content)
                path = str(file_path)
            downloaded.append(
                InboxAttachmentFile(
                    attachment_id=attachment_id,
                    filename=filename,
                    content=content,
                    content_type=meta.get("content_type"),
                    path=path,
                )
            )
        return downloaded
