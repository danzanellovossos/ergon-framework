"""
Excel connector models for ergon-framework.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ExcelFetchConfig(BaseModel):
    """Configuration for fetching transactions from an Excel file."""

    file_path: str = Field(..., description="Path to the Excel file")
    filter_col: Optional[str] = Field(None, description="Column name to filter by")
    filter_val: Optional[Any] = Field(None, description="Value to filter for in filter_col")
    sheet_name: Optional[str] = Field(None, description="Sheet name to read (defaults to active sheet)")
    batch_mode: bool = Field(False, description="When True, bundle rows into single transaction")


class ExcelRow(BaseModel):
    """Represents a row from an Excel file."""

    data: Dict[str, Any] = Field(default_factory=dict)
    row_index: int = Field(..., description="Original row index in the Excel file")