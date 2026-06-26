"""Tests for detect_aicore_cu_anomaly and its helpers.

Strategy
--------
- All tests run without network access — _fetch_usage is patched.
- Sample data: btp-usage-agent/sample_data/uas_aicore_cu_may2026.json
  (real May 2026 data; dates 2026-05-01 → 2026-05-30, multiple models).

Scenarios covered
-----------------
1. Normal operation (30-day window, real sample data)
   - algorithm chosen matches the expected data shape (MAD, high-variance data)
   - anomalies are plausible (high-value days flagged)
   - summary_text is non-empty
   - per-model analysis runs for models with ≥ 5 data points
2. IQR path   — small artificial dataset (5–13 points)
3. Z-score path — symmetric artificial dataset (≥ 14 points, low CV)
4. Insufficient data — < 5 data points
5. Empty data — no AI Core CU records → graceful response
6. Sensitivity levels — "low" flags fewer anomalies than "high"
7. Unit tests for _assess_data_shape and _run_detection helpers
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load sample data
# ---------------------------------------------------------------------------
_SAMPLE_PATH = (
    Path(__file__).parent.parent / "sample_data" / "uas_aicore_cu_may2026.json"
)
_MAY_DATA: list[dict] = json.loads(_SAMPLE_PATH.read_text())


def _filter(records: list[dict], from_date: str, to_date: str) -> list[dict]:
    return [r for r in records if from_date <= r.get("startIsoDate", "") <= to_date]


def _make_side_effect(dataset: list[dict]):
    async def _fn(from_date: str, to_date: str) -> list[dict]:
        return _filter(dataset, from_date, to_date)
    return _fn


def _empty_side_effect():
    async def _fn(from_date: str, to_date: str) -> list[dict]:
        return []
    return _fn


# ---------------------------------------------------------------------------
# Import the tool and helpers
# ---------------------------------------------------------------------------
from uas_tool import (  # noqa: E402
    detect_aicore_cu_anomaly,
    _assess_data_shape,
    _run_detection,
    _SENSITIVITY_PRESETS,
)


# ---------------------------------------------------------------------------
# Shared invoke helper
# ---------------------------------------------------------------------------

async def _invoke(
    reference_date: str,
    lookback_days: int = 30,
    sensitivity: str = "medium",
    dataset: list[dict] | None = None,
    empty: bool = False,
) -> dict:
    side_effect = (
        _empty_side_effect()
        if empty
        else _make_side_effect(dataset if dataset is not None else _MAY_DATA)
    )
    with patch("uas_tool._fetch_usage", new_callable=AsyncMock) as mock:
        mock.side_effect = side_effect
        raw = await detect_aicore_cu_anomaly.ainvoke({
            "reference_date": reference_date,
            "lookback_days":  lookback_days,
            "sensitivity":    sensitivity,
        })
    return json.loads(raw)


# ===========================================================================
# Scenario 1 — Real sample data (30-day window ending 2026-05-30)
# ===========================================================================

class TestRealSampleData:
    """Smoke-test against the real May 2026 sample dataset."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-30", lookback_days=30)

    # ── Basic structure ───────────────────────────────────────────────────────

    def test_no_error_key(self, result):
        assert "error" not in result

    def test_required_keys_present(self, result):
        for key in (
            "from_date", "to_date", "sensitivity", "data_summary",
            "data_shape", "algorithm_used", "algorithm_rationale",
            "total_daily_anomalies", "per_model_anomalies",
            "per_model_skipped", "summary_text",
        ):
            assert key in result, f"Missing key: {key}"

    def test_date_range(self, result):
        assert result["from_date"] == "2026-05-01"
        assert result["to_date"]   == "2026-05-30"

    def test_sensitivity_default(self, result):
        assert result["sensitivity"] == "medium"

    # ── Data summary ──────────────────────────────────────────────────────────

    def test_records_positive(self, result):
        assert result["data_summary"]["total_cu_records"] > 0

    def test_days_with_data_positive(self, result):
        assert result["data_summary"]["days_with_data"] > 0

    def test_daily_series_sorted(self, result):
        dates = [d["date"] for d in result["data_summary"]["daily_series"]]
        assert dates == sorted(dates)

    def test_daily_series_totals_positive(self, result):
        for entry in result["data_summary"]["daily_series"]:
            assert entry["total_cu"] > 0

    # ── Data shape ────────────────────────────────────────────────────────────

    def test_data_shape_has_required_stats(self, result):
        shape = result["data_shape"]
        for stat in ("n", "mean", "std", "median", "mad", "cv", "skewness_proxy",
                     "q1", "q3", "iqr", "recommended_method", "reason"):
            assert stat in shape, f"Missing stat: {stat}"

    def test_algorithm_is_mad_for_skewed_usage_data(self, result):
        """May data is highly right-skewed (large Anthropic spikes) → MAD expected."""
        assert result["algorithm_used"] == "mad"

    # ── Anomaly results ───────────────────────────────────────────────────────

    def test_anomalies_are_list(self, result):
        assert isinstance(result["total_daily_anomalies"], list)

    def test_anomaly_fields(self, result):
        for a in result["total_daily_anomalies"]:
            for field in ("date", "value", "score", "score_type", "direction", "reason"):
                assert field in a, f"Anomaly missing field: {field}"

    def test_anomaly_direction_values(self, result):
        for a in result["total_daily_anomalies"]:
            assert a["direction"] in ("high", "low")

    def test_per_model_anomalies_is_dict(self, result):
        assert isinstance(result["per_model_anomalies"], dict)

    def test_summary_text_non_empty(self, result):
        assert len(result["summary_text"]) > 0

    def test_high_value_days_flagged(self, result):
        """
        May 2026 data contains very high days (e.g. 2026-05-01: ~11.9 CU for
        anthropic--claude-4.6-opus-1, 2026-05-04: ~16+ CU total).
        At least one anomaly should be detected in the total-daily series.
        """
        assert len(result["total_daily_anomalies"]) > 0

    def test_anomalies_sorted_by_absolute_score_descending(self, result):
        scores = [abs(a["score"]) for a in result["total_daily_anomalies"]]
        assert scores == sorted(scores, reverse=True)


# ===========================================================================
# Scenario 2 — IQR path (small artificial dataset: 8 daily points)
# ===========================================================================

def _make_aicore_record(date: str, usage: float, model: str = "test-model") -> dict:
    return {
        "serviceId": "ai-core",
        "measureId": "capacity_units",
        "startIsoDate": date,
        "usage": usage,
        "application": model,
    }


class TestIQRPath:
    """8 data points → IQR algorithm selected."""

    # Normal daily values with one clear spike
    _DATASET = [
        _make_aicore_record(f"2026-05-{d:02d}", v)
        for d, v in [
            (1, 0.10), (2, 0.12), (3, 0.11), (4, 0.13),
            (5, 0.09), (6, 0.11), (7, 0.10), (8, 5.00),   # spike on day 8
        ]
    ]

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-08", lookback_days=8, dataset=self._DATASET)

    def test_algorithm_is_iqr(self, result):
        assert result["algorithm_used"] == "iqr"

    def test_spike_day_flagged(self, result):
        flagged_dates = {a["date"] for a in result["total_daily_anomalies"]}
        assert "2026-05-08" in flagged_dates

    def test_normal_days_not_flagged(self, result):
        flagged_dates = {a["date"] for a in result["total_daily_anomalies"]}
        # Days 1-7 are all tightly clustered → none should be anomalous
        for d in range(1, 8):
            assert f"2026-05-{d:02d}" not in flagged_dates

    def test_anomaly_direction_is_high(self, result):
        spike = next(
            a for a in result["total_daily_anomalies"] if a["date"] == "2026-05-08"
        )
        assert spike["direction"] == "high"

    def test_score_type_is_iqr_distance(self, result):
        for a in result["total_daily_anomalies"]:
            assert a["score_type"] == "iqr_distance"

    def test_upper_fence_and_lower_fence_present(self, result):
        for a in result["total_daily_anomalies"]:
            assert "upper_fence" in a or "lower_fence" in a


# ===========================================================================
# Scenario 3 — Z-score path (≥ 14 data points, symmetric, low CV)
# ===========================================================================

class TestZScorePath:
    """14 low-variance points → Z-score algorithm selected."""

    # Tight cluster around 1.0 with one spike at 10.0
    _BASE = [1.0, 0.98, 1.02, 1.01, 0.99, 1.03, 0.97,
             1.00, 1.02, 0.98, 1.01, 0.99, 1.00, 1.01]
    _DATES = [f"2026-05-{i+1:02d}" for i in range(14)]
    _DATASET_NORMAL = [
        _make_aicore_record(d, v)
        for d, v in zip(_DATES, _BASE)
    ]
    # Add spike on day 15
    _DATASET_SPIKE = _DATASET_NORMAL + [_make_aicore_record("2026-05-15", 10.0)]

    @pytest.fixture
    async def result_normal(self):
        return await _invoke("2026-05-14", lookback_days=14, dataset=self._DATASET_NORMAL)

    @pytest.fixture
    async def result_spike(self):
        return await _invoke("2026-05-15", lookback_days=15, dataset=self._DATASET_SPIKE)

    def test_algorithm_is_zscore(self, result_normal):
        assert result_normal["algorithm_used"] == "zscore"

    def test_no_anomalies_in_normal_data(self, result_normal):
        assert result_normal["total_daily_anomalies"] == []

    def test_spike_flagged_in_zscore(self, result_spike):
        flagged = {a["date"] for a in result_spike["total_daily_anomalies"]}
        assert "2026-05-15" in flagged

    def test_score_type_is_zscore(self, result_spike):
        for a in result_spike["total_daily_anomalies"]:
            assert a["score_type"] == "z-score"


# ===========================================================================
# Scenario 4 — Insufficient data (< 5 points)
# ===========================================================================

class TestInsufficientData:
    """3 data points → insufficient; no anomalies, no crash."""

    _DATASET = [
        _make_aicore_record("2026-05-01", 0.5),
        _make_aicore_record("2026-05-02", 0.6),
        _make_aicore_record("2026-05-03", 0.55),
    ]

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-03", lookback_days=7, dataset=self._DATASET)

    def test_no_error_key(self, result):
        assert "error" not in result

    def test_algorithm_is_insufficient(self, result):
        assert result["algorithm_used"] == "insufficient_data"

    def test_no_anomalies_returned(self, result):
        assert result["total_daily_anomalies"] == []

    def test_summary_mentions_insufficient(self, result):
        assert "insufficient" in result["summary_text"].lower() or \
               "5" in result["summary_text"]


# ===========================================================================
# Scenario 5 — Empty data
# ===========================================================================

class TestEmptyData:
    """No records at all → graceful, no crash, no anomalies."""

    @pytest.fixture
    async def result(self):
        return await _invoke("2026-05-30", empty=True)

    def test_no_error_key(self, result):
        assert "error" not in result

    def test_no_anomalies(self, result):
        assert result["total_daily_anomalies"] == []
        assert result["per_model_anomalies"]   == {}

    def test_summary_text_non_empty(self, result):
        assert len(result["summary_text"]) > 0

    def test_no_algorithm_key_needed_but_response_valid(self, result):
        # Response is valid JSON with expected top-level keys
        assert "summary_text" in result


# ===========================================================================
# Scenario 6 — Sensitivity comparison
# ===========================================================================

class TestSensitivity:
    """
    'high' sensitivity (looser thresholds) should flag ≥ 'low' sensitivity.
    """

    @pytest.fixture
    async def low_result(self):
        return await _invoke("2026-05-30", sensitivity="low")

    @pytest.fixture
    async def high_result(self):
        return await _invoke("2026-05-30", sensitivity="high")

    def test_high_flags_at_least_as_many_as_low(self, low_result, high_result):
        low_count  = len(low_result["total_daily_anomalies"])
        high_count = len(high_result["total_daily_anomalies"])
        assert high_count >= low_count

    def test_sensitivity_reflected_in_result(self, low_result, high_result):
        assert low_result["sensitivity"]  == "low"
        assert high_result["sensitivity"] == "high"


# ===========================================================================
# Unit tests — _assess_data_shape
# ===========================================================================

class TestAssessDataShape:
    """Direct unit tests for the shape-assessment helper."""

    def test_empty_list(self):
        shape = _assess_data_shape([])
        assert shape["recommended_method"] == "insufficient_data"

    def test_single_point(self):
        shape = _assess_data_shape([1.0])
        assert shape["recommended_method"] == "insufficient_data"

    def test_four_points_insufficient(self):
        shape = _assess_data_shape([1.0, 2.0, 3.0, 4.0])
        assert shape["recommended_method"] == "insufficient_data"

    def test_five_points_iqr(self):
        shape = _assess_data_shape([1.0, 2.0, 3.0, 4.0, 5.0])
        assert shape["recommended_method"] == "iqr"

    def test_thirteen_points_iqr(self):
        vals = [float(i) for i in range(1, 14)]
        shape = _assess_data_shape(vals)
        assert shape["recommended_method"] == "iqr"

    def test_fourteen_symmetric_points_zscore(self):
        # Low CV, low skew → zscore
        vals = [1.0 + i * 0.01 for i in range(14)]   # near-constant, low CV
        shape = _assess_data_shape(vals)
        assert shape["recommended_method"] == "zscore"

    def test_high_cv_selects_mad(self):
        # Mix of near-zero and large values → high CV → MAD
        vals = [0.01] * 10 + [100.0] * 4
        shape = _assess_data_shape(vals)
        assert shape["recommended_method"] == "mad"

    def test_cv_computed_correctly(self):
        import statistics
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        shape = _assess_data_shape(vals)
        expected_cv = statistics.stdev(vals) / statistics.mean(vals)
        assert math.isclose(shape["cv"], round(expected_cv, 4), rel_tol=1e-4)

    def test_n_matches_input_length(self):
        vals = [0.5] * 20
        shape = _assess_data_shape(vals)
        assert shape["n"] == 20

    def test_reason_field_present(self):
        shape = _assess_data_shape([1.0] * 7)
        assert "reason" in shape and len(shape["reason"]) > 0


# ===========================================================================
# Unit tests — _run_detection
# ===========================================================================

class TestRunDetection:
    """Unit tests for the anomaly detection runner."""

    _THRESH = _SENSITIVITY_PRESETS["medium"]

    def _series(self, values: list[float], start_day: int = 1) -> list[tuple[str, float]]:
        return [(f"2026-05-{start_day + i:02d}", v) for i, v in enumerate(values)]

    # ── Z-score ───────────────────────────────────────────────────────────────

    def test_zscore_no_anomalies_in_uniform_data(self):
        vals   = [1.0] * 16
        shape  = _assess_data_shape(vals)
        result = _run_detection(self._series(vals), shape, self._THRESH)
        assert result == []   # std=0 → no anomalies

    def test_zscore_flags_spike(self):
        vals = [1.0] * 14 + [20.0]
        shape = _assess_data_shape(vals)
        shape["recommended_method"] = "zscore"   # force path for test
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        assert any(a["date"] == "2026-05-15" for a in result)

    # ── IQR ──────────────────────────────────────────────────────────────────

    def test_iqr_flags_high_outlier(self):
        vals   = [1.0] * 6 + [50.0]     # 7 values, IQR path
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        assert any(a["direction"] == "high" for a in result)

    def test_iqr_score_type(self):
        vals   = [1.0] * 5 + [100.0]
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        for a in result:
            assert a["score_type"] == "iqr_distance"

    # ── MAD ──────────────────────────────────────────────────────────────────

    def test_mad_flags_spike_in_skewed_data(self):
        vals   = [0.01] * 12 + [50.0] * 2 + [0.01] * 2   # high CV
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        assert len(result) > 0

    def test_mad_score_type(self):
        vals   = [0.01] * 12 + [50.0, 0.01, 0.01, 0.01]
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        for a in result:
            assert a["score_type"] == "modified_z-score (MAD)"

    # ── Insufficient data ─────────────────────────────────────────────────────

    def test_insufficient_data_returns_empty(self):
        vals   = [1.0, 2.0, 3.0]
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        assert result == []

    # ── Sort order ────────────────────────────────────────────────────────────

    def test_results_sorted_by_absolute_score_descending(self):
        vals   = [0.01] * 10 + [100.0, 50.0] + [0.01] * 2
        shape  = _assess_data_shape(vals)
        series = self._series(vals)
        result = _run_detection(series, shape, self._THRESH)
        if len(result) > 1:
            scores = [abs(a["score"]) for a in result]
            assert scores == sorted(scores, reverse=True)
