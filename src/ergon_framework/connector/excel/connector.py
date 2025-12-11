"""
Excel connector for ergon-framework.
Provides transaction-based access to Excel files with support for:
- Batch mode (multiple rows per transaction)
- Normal mode (one row per transaction)
- Sharding for parallel processing
"""

import logging
import uuid
from typing import Any, List, Optional

from ..connector import Connector
from ..transaction import Transaction
from .service import ExcelService

logger = logging.getLogger(__name__)


class ExcelConnector(Connector):
    """
    Connector for reading transactions from Excel files.
    Features:
    - Iterator-based reading (memory efficient)
    - Batch mode for high-throughput processing
    - Automatic sharding support for parallel workers
    Example:
        service = ExcelService()
        connector = ExcelConnector(service)
        transactions = connector.fetch_transactions(
            limit=100,
            file_path="data.xlsx",
            filter_col="Type",
            filter_val="Active",
            batch_mode=True,
        )
    """

    def __init__(self, service: ExcelService):
        """
        Initialize the Excel connector.
        Args:
            service: ExcelService instance for reading Excel files.
        """
        self.service = service
        self._iterators = {}

    def fetch_transactions(
        self,
        limit: int = 10,
        file_path: Optional[str] = None,
        filter_col: Optional[str] = None,
        filter_val: Optional[Any] = None,
        metadata: Optional[dict] = None,
        worker_id: Optional[int] = None,
        max_workers: Optional[int] = None,
        batch_mode: bool = False,
        sheet_name: Optional[str] = None,
        *args,
        **kwargs,
    ) -> List[Transaction]:
        """
        Fetch transactions from Excel file.
        Args:
            limit: Number of rows to fetch per call (batch size).
            file_path: Path to Excel file.
            filter_col: Column to filter by.
            filter_val: Value to filter for.
            metadata: Metadata to attach to transactions.
            worker_id: Worker ID for sharding (0-indexed).
            max_workers: Total number of workers for sharding.
            batch_mode: When True, returns a single transaction with payload
                        containing a list of rows instead of individual transactions.
                        This enables batch processing (e.g., batch embedding, batch upsert).
            sheet_name: Optional sheet name to read.
        Returns:
            List of Transaction objects.
        """
        if not file_path:
            return []

        # Create a key to identify this specific data stream
        key = (file_path, filter_col, filter_val, worker_id, max_workers, sheet_name)

        # Create iterator if needed
        if key not in self._iterators:
            data = self.service.read_excel(
                file_path,
                filter_col,
                filter_val,
                shard_id=worker_id,
                total_shards=max_workers,
                sheet_name=sheet_name,
            )
            self._iterators[key] = iter(data)

        iterator = self._iterators[key]
        base_metadata = metadata or {}

        if batch_mode:
            return self._fetch_batch_mode(iterator, key, limit, base_metadata)
        else:
            return self._fetch_normal_mode(iterator, key, limit, base_metadata)

    def _fetch_batch_mode(
        self,
        iterator,
        key,
        limit: int,
        base_metadata: dict,
    ) -> List[Transaction]:
        """Bundle multiple rows into a single transaction."""
        rows = []
        try:
            for _ in range(limit):
                row = next(iterator)
                rows.append(row)
        except StopIteration:
            pass

        if not rows:
            if key in self._iterators:
                del self._iterators[key]
            return []

        batch_id = str(uuid.uuid4())
        return [
            Transaction(
                id=batch_id,
                payload=rows,
                metadata={**base_metadata, "batch_mode": True, "batch_size": len(rows)},
            )
        ]

    def _fetch_normal_mode(
        self,
        iterator,
        key,
        limit: int,
        base_metadata: dict,
    ) -> List[Transaction]:
        """Return one transaction per row."""
        transactions = []
        try:
            for _ in range(limit):
                row = next(iterator)
                row_id = str(uuid.uuid4())
                transactions.append(
                    Transaction(
                        id=row_id,
                        payload=row,
                        metadata=base_metadata,
                    )
                )
        except StopIteration:
            pass

        if not transactions:
            if key in self._iterators:
                del self._iterators[key]

        return transactions

    def dispatch_transactions(
        self,
        transactions: List[Transaction],
        *args,
        **kwargs,
    ) -> None:
        """
        Excel connector does not support writing transactions.
        Raises:
            NotImplementedError: Always raised.
        """
        raise NotImplementedError("ExcelConnector does not support producing transactions.")
