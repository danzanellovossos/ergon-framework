"""Tests for ergon.task.utils — backoff computation and sleep helpers."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from ergon.task.utils import _get_wake_time_iso, backoff, backoff_async, compute_backoff

# =====================================================================
#   compute_backoff
# =====================================================================


class TestComputeBackoff:
    """Pure-math tests — no I/O, no mocking needed."""

    def test_zero_backoff_returns_zero(self):
        assert compute_backoff(backoff=0, multiplier=2, cap=10, attempt=5) == 0

    def test_zero_backoff_ignores_cap_and_multiplier(self):
        for attempt in range(5):
            assert compute_backoff(backoff=0, multiplier=3, cap=100, attempt=attempt) == 0

    def test_linear_constant_delay(self):
        for attempt in range(6):
            assert compute_backoff(backoff=2.0, multiplier=1, cap=0, attempt=attempt) == 2.0

    def test_linear_with_cap(self):
        for attempt in range(6):
            assert compute_backoff(backoff=2.0, multiplier=1, cap=5, attempt=attempt) == 2.0

    def test_exponential_growth(self):
        expected = [1, 2, 4, 8, 16]
        for attempt, exp in enumerate(expected):
            assert compute_backoff(backoff=1, multiplier=2, cap=0, attempt=attempt) == exp

    def test_exponential_with_cap(self):
        # safe_attempt = floor(log2(5/1)) = 2 → delay = 1*2^2 = 4, min(4,5) = 4
        result = compute_backoff(backoff=1, multiplier=2, cap=5, attempt=10)
        assert result == 4

    def test_cap_equal_to_backoff(self):
        for attempt in range(6):
            assert compute_backoff(backoff=3.0, multiplier=2, cap=3.0, attempt=attempt) == 3.0

    def test_cap_below_backoff(self):
        result = compute_backoff(backoff=1, multiplier=2, cap=0.5, attempt=0)
        assert result == 0.5

    def test_no_cap_unbounded_growth(self):
        result = compute_backoff(backoff=1, multiplier=2, cap=0, attempt=20)
        assert result == 2**20

    def test_attempt_zero_returns_base(self):
        assert compute_backoff(backoff=5, multiplier=3, cap=0, attempt=0) == 5

    def test_large_attempt_clamped_by_safe_attempt(self):
        result = compute_backoff(backoff=1, multiplier=2, cap=10, attempt=1000)
        assert result <= 10

    def test_fractional_backoff(self):
        result = compute_backoff(backoff=0.1, multiplier=2, cap=0, attempt=3)
        assert result == pytest.approx(0.8)

    def test_fractional_multiplier(self):
        result = compute_backoff(backoff=10, multiplier=1.5, cap=0, attempt=2)
        assert result == pytest.approx(10 * 1.5**2)

    def test_cap_exactly_at_growth_boundary(self):
        result = compute_backoff(backoff=1, multiplier=2, cap=8, attempt=3)
        assert result == 8
        result_over = compute_backoff(backoff=1, multiplier=2, cap=8, attempt=4)
        assert result_over == 8


# =====================================================================
#   _get_wake_time_iso
# =====================================================================


class TestGetWakeTimeIso:
    """Freeze time.time to make the output deterministic."""

    def test_returns_iso_string_for_future_time(self):
        frozen_now = 1_700_000_000.0
        delay = 5.0
        expected = datetime.fromtimestamp(frozen_now + delay).isoformat()

        with patch("ergon.task.utils.time.time", return_value=frozen_now):
            assert _get_wake_time_iso(delay) == expected

    def test_zero_delay_returns_current_time(self):
        frozen_now = 1_700_000_000.0
        expected = datetime.fromtimestamp(frozen_now).isoformat()

        with patch("ergon.task.utils.time.time", return_value=frozen_now):
            assert _get_wake_time_iso(0) == expected


# =====================================================================
#   backoff (sync)
# =====================================================================


class TestBackoffSync:
    """Patch time.sleep to avoid real delays."""

    @patch("ergon.task.utils.time.sleep")
    def test_sleeps_for_computed_delay(self, mock_sleep):
        backoff(backoff=1.0, multiplier=2, cap=0, attempt=2)
        mock_sleep.assert_called_once_with(4.0)

    @patch("ergon.task.utils.time.sleep")
    def test_no_sleep_when_delay_is_zero(self, mock_sleep):
        backoff(backoff=0, multiplier=2, cap=10, attempt=5)
        mock_sleep.assert_not_called()

    @patch("ergon.task.utils.time.sleep")
    def test_respects_cap(self, mock_sleep):
        # safe_attempt = floor(log2(3/1)) = 1 → delay = 1*2^1 = 2, min(2,3) = 2
        backoff(backoff=1.0, multiplier=2, cap=3.0, attempt=10)
        mock_sleep.assert_called_once_with(2.0)


# =====================================================================
#   backoff_async
# =====================================================================


class TestBackoffAsync:
    """Patch asyncio.sleep to avoid real delays."""

    @pytest.mark.asyncio
    async def test_sleeps_for_computed_delay(self):
        with patch("ergon.task.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await backoff_async(backoff=1.0, multiplier=2, cap=0, attempt=2)
            mock_sleep.assert_awaited_once_with(4.0)

    @pytest.mark.asyncio
    async def test_no_sleep_when_delay_is_zero(self):
        with patch("ergon.task.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await backoff_async(backoff=0, multiplier=2, cap=10, attempt=5)
            mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_cap(self):
        # safe_attempt = floor(log2(3/1)) = 1 → delay = 1*2^1 = 2, min(2,3) = 2
        with patch("ergon.task.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await backoff_async(backoff=1.0, multiplier=2, cap=3.0, attempt=10)
            mock_sleep.assert_awaited_once_with(2.0)
