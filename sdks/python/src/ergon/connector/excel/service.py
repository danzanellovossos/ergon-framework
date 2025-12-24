"""
Excel service (client) for reading Excel files.
"""

import logging
from typing import Any, Generator, Optional

from openpyxl import load_workbook

logger = logging.getLogger(__name__)


class ExcelService:
    """
    Service for reading Excel files with streaming support.
    Supports:
    - Streaming row-by-row reading (memory efficient)
    - Column-based filtering
    - Sharding for parallel processing
    """

    def __init__(self):
        # Cache for loaded data (optional future enhancement)
        self._cache = {}

    def read_excel(
        self,
        file_path: str,
        filter_col: Optional[str] = None,
        filter_val: Optional[Any] = None,
        shard_id: Optional[int] = None,
        total_shards: Optional[int] = None,
        sheet_name: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """
        Read Excel file and yield rows as dictionaries.
        Args:
            file_path: Path to the Excel file.
            filter_col: Optional column name to filter by.
            filter_val: Optional value to filter for in filter_col.
            shard_id: Worker shard ID (0-indexed). Used for parallel processing.
            total_shards: Total number of shards/workers. Used for parallel processing.
            sheet_name: Optional sheet name to read (defaults to active sheet).
        When shard_id and total_shards are provided, only rows where
        (matched_index % total_shards == shard_id) are yielded. This allows
        multiple workers to process different partitions of the same file.
        Yields:
            dict: Row data as a dictionary with column headers as keys.
        """
        wb = load_workbook(filename=file_path, read_only=True, data_only=True)
        try:
            # Select sheet
            if sheet_name:
                ws = wb[sheet_name]
            else:
                ws = wb.active

            rows = ws.rows

            try:
                headers = [cell.value for cell in next(rows)]
            except StopIteration:
                return

            sharding_enabled = shard_id is not None and total_shards is not None
            matched_index = 0
            yielded_count = 0

            logger.debug(f"Sharding enabled: {sharding_enabled}")
            if sharding_enabled:
                logger.info(f"[EXCEL] Worker {shard_id}/{total_shards} starting to read {file_path}")

            for row in rows:
                row_values = [cell.value for cell in row]
                row_dict = dict(zip(headers, row_values))

                # Apply filter first
                if filter_col and filter_val:
                    val = row_dict.get(filter_col)
                    if val != filter_val:
                        continue

                # Apply sharding after filtering
                if sharding_enabled and (matched_index % total_shards != shard_id):
                    matched_index += 1
                    continue

                matched_index += 1
                yielded_count += 1
                yield row_dict

            if sharding_enabled:
                logger.info(f"[EXCEL] Worker {shard_id} finished. Matched={matched_index}, Yielded={yielded_count}")
        finally:
            wb.close()
