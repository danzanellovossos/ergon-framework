"""
Excel connector for ergon-framework.
Provides transaction-based access to Excel files with support for:
- Batch mode (multiple rows per transaction for high-throughput processing)
- Normal mode (one row per transaction)
- Sharding for parallel processing
"""

from .connector import ExcelConnector
from .models import ExcelFetchConfig, ExcelRow
from .service import ExcelService

__all__ = [
    "ExcelConnector",
    "ExcelService",
    "ExcelFetchConfig",
    "ExcelRow",
]
