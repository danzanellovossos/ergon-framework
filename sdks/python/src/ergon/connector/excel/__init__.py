"""
Excel connector for ergon-framework.
Provides transaction-based access to Excel files with support for:
- Batch mode (multiple rows per transaction for high-throughput processing)
- Normal mode (one row per transaction)
- Sharding for parallel processing
"""

from typing import TYPE_CHECKING, Any

from .._lazy import exported_names, load_optional_export
from .models import ExcelFetchConfig, ExcelRow

if TYPE_CHECKING:
    from .connector import ExcelConnector
    from .service import ExcelService

_LAZY_EXPORTS = {
    "ExcelConnector": "connector",
    "ExcelService": "service",
}


def __getattr__(name: str) -> Any:
    return load_optional_export(
        name=name,
        package=__name__,
        exports=_LAZY_EXPORTS,
        namespace=globals(),
        extra="excel",
        dependencies=("openpyxl",),
    )


def __dir__() -> list[str]:
    return exported_names(globals(), _LAZY_EXPORTS)


__all__ = [
    "ExcelConnector",
    "ExcelService",
    "ExcelFetchConfig",
    "ExcelRow",
]
