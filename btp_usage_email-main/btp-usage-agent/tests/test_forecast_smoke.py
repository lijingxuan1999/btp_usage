"""Smoke tests for simulate_aicore_cu_eom_forecast using sample data.

Strategy
--------
- Load uas_aicore_cu_may2026.json from sample_data/ (real recorded data for
  May 2026, dates 2026-05-01 → 2026-05-30).
- Patch uas_tool._fetch_usage to return records filtered by the requested date
  range so no real HTTP calls are ever made.
- Cover five scenarios:
    1. Mid-month  (May 16)  — 16 days elapsed, no prior-month data
                              → linear + trend only, historical null
    2. Late-month (May 30)  — 30 days elapsed, 1 day remaining
    3. First day  (May  1)  — single day of data; trend = linear
    4. With synthetic April — historical ratio method becomes active
    5. Empty data           — all forecasts null, no crash
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Sample data — loaded once at module import time
# ---------------------------------------------------------------------------
_SAMPLE_PATH = (
    Path(__file__).parent.parent / "sample_data" / "uas_aicore_cu_may2026.json"
)
_MAY_DATA: list[dict] = json.loads(_SAMPLE_PATH.read_text())


def _filter(records: list[dict], from_date: str, to_date: str) -> list[dict]:
    """Return records whose startIsoDate falls within [from_date, to_date]."""
    return [r for r in records if from_date <= r.get("startIsoDate", "") <= to_date]


def _april_from_may(may_records: list[dict]) -> list[dict]:
    """Synthetic April 2026 data: shift every May date back one month.

    Used to exercise the historical-ratio forecast method (Method 3).
    April has 30 days so days 01-30 all map cleanly.
    """
    result = []
    for r in may_records:
        iso = r.get("startIsoDate", "")
        if iso.startswith("2026-05-"):
            day = iso[8:]  # "01" … "31"
            if int(day) <= 30:
                result.append(
                    {
                        **r,
                        "startIsoDate": f"2026-04-{day}",
                        "endIsoDate": f"2026-04-{day}",
                    }
                )
    return result


_APRIL_DATA: list[dict] = _april_from_may(_MAY_DATA)


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------

def _make_side_effect(extra: list[dict] | None = None):
    """AsyncMock side-effect that filters the combined dataset by date range."""
    combined = _MAY_DATA + (extra or [])

    async def _fn(from_date: str, to_date: str) -> list[dict]:
        return _filter(combined, from_date, to_date)

    return _fn


def _empty_side_effect():
    """AsyncMock side-effect that always returns no records."""
    async def _fn(from_date: str, to_date: str) -> list[dict]:
        return []
    return _fn


# ---------------------------------------------------------------------------
# Import the tool (app/ is on sys.path via tests/conftest.py)
# ---------------------------------------------------------------------------
from uas_tool import simulate_aicore_cu_eom_forecast  # noqa: E402


# ---------------------------------------------------------------------------
# Shared async helper
# ---------------------------------------------------------------------------

async def _invoke(reference_date: str, extra: list[dict] | None = None, empty: bool = False) -> dict:
    """Invoke the forecast tool with mocked _fetch_usage and return parsed JSON."""
    side_effect = _empty_side_effect() if empty else _make_side_effect(extra)
    with patch("uas_tool._fetch_usage", new_callable=AsyncMock) as mock:
        mock.side_effect = side_effect
        raw = await simulate_aicore_cu_eom_forecast.ainvoke(
            {"reference_date": reference_date}
        )
    return json.loads(raw)


# ===========================================================================
# Scenario 1 — Mid-month: reference_date 2026-05-16
#   May has 31 days; 16 elapsed, 15 remaining; no April data → historical null
# ===========================================================================

class TestMidMonth:
    """16 days of data, 15 remaining, no prior-month data."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-16")

    # ── Context ──────────────────────────────────────────────────────────────

    def test_all_context_keys_present(self, result):
        expected = {
            "reference_date", "month_start", "month_end",
            "days_in_month", "last_data_date",
            "data_days_elapsed", "days_remaining",
        }
        assert expected <= set(result["context"])

    def test_context_reference_date(self, result):
        assert result["context"]["reference_date"] == "2026-05-16"

    def test_context_month_start_and_end(self, result):
        assert result["context"]["month_start"] == "2026-05-01"
        assert result["context"]["month_end"]   == "2026-05-31"

    def test_context_days_in_month(self, result):
        assert result["context"]["days_in_month"] == 31        # May has 31 days

    def test_context_last_data_date(self, result):
        assert result["context"]["last_data_date"] == "2026-05-16"

    def test_context_days_elapsed(self, result):
        assert result["context"]["data_days_elapsed"] == 16

    def test_context_days_remaining(self, result):
        assert result["context"]["days_remaining"] == 15

    # ── Current-month totals ─────────────────────────────────────────────────

    def test_cu_so_far_positive(self, result):
        assert result["current_month"]["cu_so_far"] > 0

    def test_daily_breakdown_covers_all_16_dates(self, result):
        dates = {d["date"] for d in result["current_month"]["daily_breakdown"]}
        assert len(dates) == 16
        assert min(dates) == "2026-05-01"
        assert max(dates) == "2026-05-16"

    def test_daily_breakdown_sum_equals_cu_so_far(self, result):
        total = sum(d["cu"] for d in result["current_month"]["daily_breakdown"])
        assert math.isclose(total, result["current_month"]["cu_so_far"], rel_tol=1e-5)

    def test_by_model_sum_equals_cu_so_far(self, result):
        total = sum(m["cu_so_far"] for m in result["current_month"]["by_model"])
        assert math.isclose(total, result["current_month"]["cu_so_far"], rel_tol=1e-5)

    def test_by_model_sorted_descending(self, result):
        values = [m["cu_so_far"] for m in result["current_month"]["by_model"]]
        assert values == sorted(values, reverse=True)

    def test_record_count_positive(self, result):
        assert result["record_count"] > 0

    # ── Linear forecast ──────────────────────────────────────────────────────

    def test_linear_present(self, result):
        assert result["forecasts"]["linear"] is not None

    def test_linear_exceeds_cu_so_far(self, result):
        assert result["forecasts"]["linear"]["forecast_cu"] > result["current_month"]["cu_so_far"]

    def test_linear_math(self, result):
        """forecast_cu = cu_so_far + avg_daily_rate × days_remaining"""
        f   = result["forecasts"]["linear"]
        ctx = result["context"]
        cu  = result["current_month"]["cu_so_far"]
        expected = round(cu + f["avg_daily_rate"] * ctx["days_remaining"], 6)
        assert math.isclose(f["forecast_cu"], expected, rel_tol=1e-5)

    # ── Trend 7d forecast ────────────────────────────────────────────────────

    def test_trend_7d_present(self, result):
        assert result["forecasts"]["trend_7d"] is not None

    def test_trend_7d_uses_7_days(self, result):
        assert result["forecasts"]["trend_7d"]["recent_days_used"] == 7

    def test_trend_7d_positive(self, result):
        assert result["forecasts"]["trend_7d"]["forecast_cu"] > 0

    def test_trend_7d_math(self, result):
        """forecast_cu = cu_so_far + recent_daily_rate × days_remaining"""
        t   = result["forecasts"]["trend_7d"]
        ctx = result["context"]
        cu  = result["current_month"]["cu_so_far"]
        expected = round(cu + t["recent_7d_daily_rate"] * ctx["days_remaining"], 6)
        assert math.isclose(t["forecast_cu"], expected, rel_tol=1e-5)

    # ── Historical: null (no April data) ────────────────────────────────────

    def test_historical_null_without_prior_data(self, result):
        assert result["forecasts"]["historical"] is None

    # ── Ensemble ─────────────────────────────────────────────────────────────

    def test_ensemble_present(self, result):
        assert result["forecasts"]["ensemble"] is not None

    def test_ensemble_methods_are_linear_and_trend(self, result):
        methods = set(result["forecasts"]["ensemble"]["methods_combined"])
        assert methods == {"linear", "trend_7d"}

    def test_ensemble_is_equal_weight_average(self, result):
        """Without historical, linear and trend have equal weight → simple mean."""
        f_lin = result["forecasts"]["linear"]["forecast_cu"]
        f_tr  = result["forecasts"]["trend_7d"]["forecast_cu"]
        expected = round((f_lin + f_tr) / 2, 6)
        assert math.isclose(
            result["forecasts"]["ensemble"]["forecast_cu"], expected, rel_tol=1e-5
        )

    def test_ensemble_forecast_positive(self, result):
        assert result["forecasts"]["ensemble"]["forecast_cu"] > 0


# ===========================================================================
# Scenario 2 — Late-month: reference_date 2026-05-30
#   30 days elapsed, 1 remaining; sample has data through May 30
# ===========================================================================

class TestLateMonth:
    """30 days elapsed, 1 day remaining."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-30")

    def test_days_elapsed_is_30(self, result):
        assert result["context"]["data_days_elapsed"] == 30

    def test_days_remaining_is_1(self, result):
        assert result["context"]["days_remaining"] == 1

    def test_last_data_date_is_may_30(self, result):
        assert result["context"]["last_data_date"] == "2026-05-30"

    def test_linear_forecast_is_cu_plus_one_avg_day(self, result):
        """With 1 day left: forecast = cu_so_far + 1 × avg_daily_rate."""
        f   = result["forecasts"]["linear"]
        cu  = result["current_month"]["cu_so_far"]
        expected = round(cu + f["avg_daily_rate"], 6)
        assert math.isclose(f["forecast_cu"], expected, rel_tol=1e-5)

    def test_ensemble_positive(self, result):
        assert result["forecasts"]["ensemble"]["forecast_cu"] > 0

    def test_no_error_key(self, result):
        assert "error" not in result


# ===========================================================================
# Scenario 3 — First day: reference_date 2026-05-01
#   Only 1 day of data; trend window = 1 = whole month → trend == linear
# ===========================================================================

class TestFirstDay:
    """1 day of data, 30 days remaining."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-01")

    def test_days_elapsed_is_1(self, result):
        assert result["context"]["data_days_elapsed"] == 1

    def test_days_remaining_is_30(self, result):
        assert result["context"]["days_remaining"] == 30

    def test_daily_breakdown_has_exactly_1_entry(self, result):
        assert len(result["current_month"]["daily_breakdown"]) == 1
        assert result["current_month"]["daily_breakdown"][0]["date"] == "2026-05-01"

    def test_trend_uses_single_day(self, result):
        assert result["forecasts"]["trend_7d"]["recent_days_used"] == 1

    def test_linear_and_trend_identical_when_one_day(self, result):
        """With a single data point the 7d window = 1 day → same rate as linear."""
        f_lin = result["forecasts"]["linear"]["forecast_cu"]
        f_tr  = result["forecasts"]["trend_7d"]["forecast_cu"]
        assert math.isclose(f_lin, f_tr, rel_tol=1e-5)

    def test_historical_null(self, result):
        assert result["forecasts"]["historical"] is None

    def test_ensemble_positive(self, result):
        assert result["forecasts"]["ensemble"]["forecast_cu"] > 0


# ===========================================================================
# Scenario 4 — With synthetic April data
#   Historical ratio method becomes active; ensemble uses all three methods
# ===========================================================================

class TestWithPreviousMonthData:
    """Synthetic April data enables the historical-ratio forecast."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-16", extra=_APRIL_DATA)

    def test_historical_present(self, result):
        assert result["forecasts"]["historical"] is not None

    def test_historical_prev_month_label(self, result):
        assert result["forecasts"]["historical"]["prev_month_label"] == "2026-04"

    def test_historical_prev_full_cu_positive(self, result):
        assert result["forecasts"]["historical"]["prev_full_cu"] > 0

    def test_historical_prev_partial_cu_positive(self, result):
        assert result["forecasts"]["historical"]["prev_partial_cu"] > 0

    def test_historical_ratio_positive(self, result):
        assert result["forecasts"]["historical"]["prev_month_ratio"] > 0

    def test_historical_forecast_math(self, result):
        """forecast_cu = cu_so_far × prev_month_ratio"""
        h  = result["forecasts"]["historical"]
        cu = result["current_month"]["cu_so_far"]
        expected = round(cu * h["prev_month_ratio"], 6)
        assert math.isclose(h["forecast_cu"], expected, rel_tol=1e-5)

    def test_previous_month_partial_days_matches_elapsed(self, result):
        """prev_partial_days should equal data_days_elapsed (same cutoff day)."""
        assert result["previous_month"]["prev_partial_days"] == result["context"]["data_days_elapsed"]

    def test_ensemble_includes_all_three_methods(self, result):
        methods = set(result["forecasts"]["ensemble"]["methods_combined"])
        assert methods == {"linear", "trend_7d", "historical"}

    def test_ensemble_historical_weighted_correctly(self, result):
        """data_days_elapsed=16 >= 14 → historical weight = 1.4, others = 1.0."""
        f_lin  = result["forecasts"]["linear"]["forecast_cu"]
        f_tr   = result["forecasts"]["trend_7d"]["forecast_cu"]
        f_hist = result["forecasts"]["historical"]["forecast_cu"]
        # weights: lin=1.0, trend=1.0, hist=1.4
        expected = round((f_lin * 1.0 + f_tr * 1.0 + f_hist * 1.4) / 3.4, 6)
        assert math.isclose(
            result["forecasts"]["ensemble"]["forecast_cu"], expected, rel_tol=1e-5
        )

    def test_ensemble_forecast_positive(self, result):
        assert result["forecasts"]["ensemble"]["forecast_cu"] > 0


# ===========================================================================
# Scenario 5 — Empty data: graceful degradation
#   All API calls return []; no crash; all forecasts null
# ===========================================================================

class TestEmptyData:
    """No records → all forecasts must be null, no exception raised."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-16", empty=True)

    def test_no_error_key(self, result):
        assert "error" not in result

    def test_cu_so_far_is_zero(self, result):
        assert result["current_month"]["cu_so_far"] == 0

    def test_daily_breakdown_empty(self, result):
        assert result["current_month"]["daily_breakdown"] == []

    def test_record_count_zero(self, result):
        assert result["record_count"] == 0

    def test_all_forecasts_null(self, result):
        for method in ("linear", "trend_7d", "historical", "ensemble"):
            assert result["forecasts"][method] is None, f"Expected {method} to be null"

    def test_previous_month_full_cu_zero(self, result):
        assert result["previous_month"]["prev_full_cu"] == 0
