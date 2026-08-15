import logging
import re
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Literal, Optional, Union

from ....transaction import Transaction
from ..adapters import ActivityAdapter
from ..models import InboxAttachmentFile

logger = logging.getLogger(__name__)


class ChannelsAttachmentService:
    """Hydrate or download inbound attachment files."""

    _MAX_CONCURRENT_DOWNLOADS = 4

    def __init__(
        self,
        client: Any,
        download_client: Optional[Any] = None,
    ) -> None:
        self._client = download_client or client

    def download_bytes(
        self,
        config_id: str,
        event_id: str,
        attachment_id: str,
    ) -> bytes:
        """Download the bytes of the attachment."""
        return self._client.channels.configs.activity_attachment_file(
            config_id,
            event_id,
            attachment_id,
        )

    def _download_attachment(
        self,
        config_id: str,
        event_id: str,
        attachment_id: str,
        filename: str,
    ) -> bytes:
        logger.info(
            "Downloading attachment %s (%s)",
            filename,
            attachment_id,
        )
        return self.download_bytes(config_id, event_id, attachment_id)

    def hydrate_inbox_attachments(
        self,
        config_id: str,
        transaction: Transaction,
        failure_policy: Literal["raise", "best_effort"] = "raise",
    ) -> Transaction:
        """Hydrate the inbox attachments."""
        return self.hydrate_inbox_transactions(
            config_id,
            [transaction],
            failure_policy=failure_policy,
        )[0]

    def hydrate_inbox_transactions(
        self,
        config_id: str,
        transactions: List[Transaction],
        failure_policy: Literal["raise", "best_effort"] = "raise",
    ) -> List[Transaction]:
        """Hydrate a batch with bounded concurrent attachment downloads."""
        hydrated = list(transactions)
        enriched: Dict[int, List[Dict[str, Any]]] = {}
        failures: Dict[int, List[Dict[str, str]]] = {}
        jobs: List[tuple[int, int, str, str, str]] = []

        for transaction_index, transaction in enumerate(transactions):
            event_id = transaction.id
            if not event_id:
                continue
            attachments = ActivityAdapter.attachments(transaction)
            enriched[transaction_index] = list(attachments)
            for attachment_index, metadata in enumerate(attachments):
                if metadata.get("content") is not None:
                    continue
                attachment_id = ActivityAdapter.attachment_id(metadata)
                if not attachment_id:
                    continue
                filename = str(metadata.get("filename") or attachment_id)
                jobs.append(
                    (
                        transaction_index,
                        attachment_index,
                        event_id,
                        attachment_id,
                        filename,
                    )
                )

        if not jobs:
            return hydrated

        max_workers = min(self._MAX_CONCURRENT_DOWNLOADS, len(jobs))
        future_jobs: List[tuple[int, int, str, str, str, Future[bytes]]] = []
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="channels-attachment",
        ) as executor:
            for job in jobs:
                transaction_index, attachment_index, event_id, attachment_id, filename = job
                future = executor.submit(
                    self._download_attachment,
                    config_id,
                    event_id,
                    attachment_id,
                    filename,
                )
                future_jobs.append((*job, future))

            changed: set[int] = set()
            for (
                transaction_index,
                attachment_index,
                event_id,
                attachment_id,
                filename,
                future,
            ) in future_jobs:
                try:
                    content = future.result()
                except Exception as exc:
                    if failure_policy == "raise":
                        raise
                    logger.warning(
                        "Skipping attachment %s (%s) for event %s",
                        filename,
                        attachment_id,
                        event_id,
                        exc_info=True,
                    )
                    failures.setdefault(transaction_index, []).append(
                        {
                            "attachment_id": attachment_id,
                            "filename": filename,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                original = enriched[transaction_index][attachment_index]
                enriched[transaction_index][attachment_index] = {
                    **original,
                    "content": content,
                }
                changed.add(transaction_index)

        for transaction_index in changed | failures.keys():
            transaction = ActivityAdapter.with_attachments(
                transactions[transaction_index],
                enriched[transaction_index],
            )
            transaction_failures = failures.get(transaction_index)
            if transaction_failures:
                metadata = dict(transaction.metadata or {})
                metadata["attachment_failures"] = transaction_failures
                transaction = transaction.model_copy(update={"metadata": metadata})
            hydrated[transaction_index] = transaction
        return hydrated

    @staticmethod
    def _safe_component(value: str, *, fallback: str) -> str:
        """Safe component."""
        normalized = unicodedata.normalize("NFKC", value).replace("\\", "/").strip()
        windows = PureWindowsPath(value)
        if normalized.startswith("/") or value.startswith(("\\\\", "//")) or windows.drive:
            raise ValueError(f"Unsafe absolute attachment filename: {value!r}")
        name = PurePosixPath(normalized).name
        if name in {"", ".", ".."}:
            name = fallback
        name = "".join("_" if unicodedata.category(char).startswith("C") else char for char in name)
        name = re.sub(r"[/\\:*?\"<>|]", "_", name).strip(" .")
        return name or fallback

    @classmethod
    def _output_filename(
        cls,
        filename: str,
        attachment_id: str,
    ) -> str:
        """Output filename."""
        safe_id = cls._safe_component(
            attachment_id,
            fallback="attachment",
        )
        safe_name = cls._safe_component(filename, fallback=safe_id)
        path = PurePosixPath(safe_name)
        return f"{path.stem}--{safe_id}{path.suffix}"

    def download_inbox_attachments(
        self,
        config_id: str,
        transaction: Transaction,
        dest: Optional[Union[str, Path]] = None,
    ) -> List[InboxAttachmentFile]:
        """Download the inbox attachments."""
        event_id = transaction.id
        if not event_id:
            raise ValueError("transaction.id is required to download attachments")

        dest_root = Path(dest).resolve() if dest is not None else None
        event_component = self._safe_component(
            str(event_id),
            fallback="event",
        )
        dest_dir = dest_root / event_component if dest_root is not None else None
        jobs: List[tuple[str, str, Optional[str]]] = []
        for metadata in ActivityAdapter.attachments(transaction):
            attachment_id = ActivityAdapter.attachment_id(metadata)
            if not attachment_id:
                continue
            filename = self._output_filename(
                str(metadata.get("filename") or attachment_id),
                attachment_id,
            )
            jobs.append(
                (
                    attachment_id,
                    filename,
                    metadata.get("content_type"),
                )
            )

        if not jobs:
            return []

        max_workers = min(self._MAX_CONCURRENT_DOWNLOADS, len(jobs))
        futures: List[tuple[str, str, Optional[str], Future[bytes]]] = []
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="channels-attachment",
        ) as executor:
            for attachment_id, filename, content_type in jobs:
                future = executor.submit(
                    self._download_attachment,
                    config_id,
                    event_id,
                    attachment_id,
                    filename,
                )
                futures.append(
                    (
                        attachment_id,
                        filename,
                        content_type,
                        future,
                    )
                )

        downloaded: List[InboxAttachmentFile] = []
        for attachment_id, filename, content_type, future in futures:
            content = future.result()
            path: Optional[str] = None
            if dest_dir is not None:
                dest_dir.mkdir(parents=True, exist_ok=True)
                resolved_dir = dest_dir.resolve()
                file_path = (resolved_dir / filename).resolve()
                try:
                    file_path.relative_to(resolved_dir)
                except ValueError as exc:
                    raise ValueError("Attachment destination escapes the event directory") from exc
                file_path.write_bytes(content)
                path = str(file_path)
            downloaded.append(
                InboxAttachmentFile(
                    attachment_id=attachment_id,
                    filename=filename,
                    content=content,
                    content_type=content_type,
                    path=path,
                )
            )
        return downloaded
