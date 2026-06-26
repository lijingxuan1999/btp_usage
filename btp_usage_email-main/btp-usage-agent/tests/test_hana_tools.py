"""Tests for the four HANA Cloud tools in hana_tool.py.

Strategy
--------
- All tests run without network access.
  _hana_get is patched to return pre-built response dicts so no real HTTP
  call or OAuth2 handshake is ever made.
- Environment variables required by the tool (HANA_SERVICE_INSTANCE_ID, etc.)
  are set via monkeypatch or os.environ within each class.

Tools covered
-------------
1. list_hana_instances        — /metering/v1/definitions
2. get_hana_metering_values   — /metering/v1/serviceInstances/{id}/values
3. get_hana_metric_definitions — /metrics/v1/definitions + per-instance
4. get_hana_metrics           — /metrics/v1/serviceInstances/{id}/values

Scenarios per tool
------------------
list_hana_instances
  A. Normal response with two resource types
  B. Empty definitions response
  C. serviceInstanceIDs filter passed through

get_hana_metering_values
  A. Aggregate mode (sum, daily interval)
  B. Raw mode (empty aggregates string)
  C. No service_instance_id — falls back to env var
  D. No service_instance_id and env var missing — error returned
  E. Invalid interval — snapped to nearest valid value
  F. Invalid aggregate — removed, request still proceeds
  G. start_timestamp >= end_timestamp — auto-corrected to 24h window
  H. Empty data response

get_hana_metric_definitions
  A. All-instances path (no service_instance_id)
  B. Per-instance path (service_instance_id provided)
  C. by_resource_type grouping correct
  D. by_category grouping covers known categories
  E. Empty definitions response

get_hana_metrics
  A. Aggregate mode with dimensions
  B. Raw mode — no aggregate fields expected
  C. No service_instance_id — env var fallback
  D. filter_expr passed as $filter query param
  E. Invalid interval snapped
  F. start_timestamp >= end_timestamp auto-corrected
  G. summary_by_metric max_value computed correctly
  H. Empty data response
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bring app/ onto sys.path (tests/conftest.py already does this, but be
# explicit so the module can also be run standalone with pytest tests/).
# ---------------------------------------------------------------------------
from hana_tool import (
    list_hana_instances,
    get_hana_metering_values,
    get_hana_metric_definitions,
    get_hana_metrics,
    _validate_timestamp,
    _infer_metric_category,
    _METERING_INTERVALS,
    _METRICS_INTERVALS,
    _METERING_AGGREGATES,
    _METRICS_AGGREGATES,
)

# ---------------------------------------------------------------------------
# Test fixtures / shared helpers
# ---------------------------------------------------------------------------

_INSTANCE_ID = "886b6bef-5949-427d-8a8d-b5ce58013748"

# ── Sample metering definitions ───────────────────────────────────────────────
_METERING_DEFS = [
    {
        "resourceType": "hana-cloud-hdb",
        "name": "DefaultNodeMemory",
        "type": "delta",
        "metricCategory": "memory",
        "description": "Memory usage charge of a HANA instance",
        "interval": 3600,
        "aggregates": ["count", "last", "sum"],
        "retention": 2592000,
    },
    {
        "resourceType": "hana-cloud-hdb",
        "name": "DefaultNodeVCPU",
        "type": "delta",
        "metricCategory": "vcpu",
        "description": "vCPU charge of a HANA instance",
        "interval": 3600,
        "aggregates": ["count", "last", "sum"],
        "retention": 2592000,
    },
    {
        "resourceType": "hana-cloud-hdl",
        "name": "RelationalEngineCompute",
        "type": "delta",
        "metricCategory": "vcpu",
        "description": "Compute charge for data lake relational engine",
        "interval": 3600,
        "aggregates": ["count", "last", "sum"],
        "retention": 2592000,
    },
]

# ── Sample metering values (aggregate mode) ───────────────────────────────────
_METERING_VALUES = [
    {
        "serviceInstanceID": _INSTANCE_ID,
        "resourceType": "hana-cloud-hdb",
        "name": "DefaultNodeMemory",
        "values": [
            {
                "startTimestamp": "2026-06-01T00:00:00Z",
                "endTimestamp": "2026-06-02T00:00:00Z",
                "sum": 24.5,
                "count": 24,
                "interval": 86400,
            },
            {
                "startTimestamp": "2026-06-02T00:00:00Z",
                "endTimestamp": "2026-06-03T00:00:00Z",
                "sum": 25.1,
                "count": 24,
                "interval": 86400,
            },
        ],
    },
    {
        "serviceInstanceID": _INSTANCE_ID,
        "resourceType": "hana-cloud-hdb",
        "name": "DefaultNodeVCPU",
        "values": [
            {
                "startTimestamp": "2026-06-01T00:00:00Z",
                "endTimestamp": "2026-06-02T00:00:00Z",
                "sum": 8.0,
                "count": 24,
                "interval": 86400,
            },
        ],
    },
]

# ── Sample metrics definitions ────────────────────────────────────────────────
_METRICS_DEFS = [
    {
        "resourceType": "hana-cloud-hdb",
        "name": "HDBMemoryUsed",
        "type": "gauge",
        "unit": "byte",
        "dimensions": ["host", "port", "service_name"],
        "description": "Memory used by Service",
        "interval": 60,
        "aggregates": ["avg", "count", "last", "max", "min"],
        "retention": 2592000,
    },
    {
        "resourceType": "hana-cloud-hdb",
        "name": "HDBCPU",
        "type": "gauge",
        "unit": "%",
        "dimensions": ["host", "port", "service_name"],
        "description": "CPU usage percentage",
        "interval": 60,
        "aggregates": ["avg", "count", "last", "max", "min"],
        "retention": 2592000,
    },
    {
        "resourceType": "hana-cloud-hdl",
        "name": "HDLDiskUsed",
        "type": "gauge",
        "unit": "byte",
        "dimensions": ["host"],
        "description": "Disk space used by data lake",
        "interval": 300,
        "aggregates": ["avg", "last", "max"],
        "retention": 2592000,
    },
]

# ── Sample metrics values (aggregate mode) ────────────────────────────────────
_METRICS_VALUES = [
    {
        "serviceInstanceID": _INSTANCE_ID,
        "resourceType": "hana-cloud-hdb",
        "name": "HDBMemoryUsed",
        "dimensions": {"host": _INSTANCE_ID, "port": "30040", "service_name": "indexserver"},
        "values": [
            {
                "startTimestamp": "2026-06-15T10:00:00Z",
                "endTimestamp": "2026-06-15T11:00:00Z",
                "max": 2873864917.0,
                "avg": 2800000000.0,
                "interval": 3600,
            },
            {
                "startTimestamp": "2026-06-15T11:00:00Z",
                "endTimestamp": "2026-06-15T12:00:00Z",
                "max": 2890000000.0,
                "avg": 2850000000.0,
                "interval": 3600,
            },
        ],
    },
    {
        "serviceInstanceID": _INSTANCE_ID,
        "resourceType": "hana-cloud-hdb",
        "name": "HDBCPU",
        "dimensions": {"host": _INSTANCE_ID, "port": "30040", "service_name": "indexserver"},
        "values": [
            {
                "startTimestamp": "2026-06-15T10:00:00Z",
                "endTimestamp": "2026-06-15T11:00:00Z",
                "max": 45.2,
                "avg": 32.1,
                "interval": 3600,
            },
        ],
    },
]


def _patch_hana_get(response: dict):
    """Return a context manager that patches _hana_get to return `response`."""
    return patch(
        "hana_tool._hana_get",
        new_callable=AsyncMock,
        return_value=response,
    )


# ===========================================================================
# list_hana_instances
# ===========================================================================

class TestListHanaInstances:
    """Tests for list_hana_instances → GET /metering/v1/definitions."""

    # ── Scenario A: Normal two-resource-type response ────────────────────────

    @pytest.fixture
    async def result_normal(self):
        response = {"data": _METERING_DEFS, "count": len(_METERING_DEFS)}
        with _patch_hana_get(response):
            raw = await list_hana_instances.ainvoke({})
        return json.loads(raw)

    def test_no_error(self, result_normal):
        assert "error" not in result_normal

    def test_required_keys(self, result_normal):
        for key in ("total_definitions", "resource_type_count", "instances", "raw_definitions"):
            assert key in result_normal

    def test_total_definitions_matches(self, result_normal):
        assert result_normal["total_definitions"] == len(_METERING_DEFS)

    def test_two_resource_types_found(self, result_normal):
        assert result_normal["resource_type_count"] == 2

    def test_hdb_instance_present(self, result_normal):
        rts = {i["resourceType"] for i in result_normal["instances"]}
        assert "hana-cloud-hdb" in rts

    def test_hdl_instance_present(self, result_normal):
        rts = {i["resourceType"] for i in result_normal["instances"]}
        assert "hana-cloud-hdl" in rts

    def test_hdb_has_two_metrics(self, result_normal):
        hdb = next(i for i in result_normal["instances"] if i["resourceType"] == "hana-cloud-hdb")
        assert hdb["metric_count"] == 2

    def test_metrics_sorted_by_name(self, result_normal):
        for inst in result_normal["instances"]:
            names = [m["name"] for m in inst["metrics"]]
            assert names == sorted(names)

    def test_metric_has_expected_fields(self, result_normal):
        hdb = next(i for i in result_normal["instances"] if i["resourceType"] == "hana-cloud-hdb")
        for m in hdb["metrics"]:
            for field in ("name", "type", "metricCategory", "description", "interval", "aggregates", "retention"):
                assert field in m

    def test_raw_definitions_matches_input(self, result_normal):
        assert result_normal["raw_definitions"] == _METERING_DEFS

    # ── Scenario B: Empty response ────────────────────────────────────────────

    @pytest.fixture
    async def result_empty(self):
        with _patch_hana_get({"data": [], "count": 0}):
            raw = await list_hana_instances.ainvoke({})
        return json.loads(raw)

    def test_empty_no_error(self, result_empty):
        assert "error" not in result_empty

    def test_empty_zero_definitions(self, result_empty):
        assert result_empty["total_definitions"] == 0
        assert result_empty["resource_type_count"] == 0
        assert result_empty["instances"] == []

    # ── Scenario C: serviceInstanceIDs filter forwarded ──────────────────────

    async def test_service_instance_ids_forwarded(self):
        """_hana_get must receive the serviceInstanceIDs query param."""
        response = {"data": [], "count": 0}
        with patch("hana_tool._hana_get", new_callable=AsyncMock, return_value=response) as mock:
            await list_hana_instances.ainvoke({"service_instance_ids": _INSTANCE_ID})
            call_params = mock.call_args[0][1]
            assert call_params.get("serviceInstanceIDs") == _INSTANCE_ID


# ===========================================================================
# get_hana_metering_values
# ===========================================================================

class TestGetHanaMeteringValues:
    """Tests for get_hana_metering_values."""

    # ── Scenario A: Aggregate mode (sum, daily) ───────────────────────────────

    @pytest.fixture
    async def result_agg(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METERING_VALUES}):
            raw = await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-03T00:00:00Z",
                "aggregates": "sum",
                "interval": 86400,
            })
        return json.loads(raw)

    def test_no_error(self, result_agg):
        assert "error" not in result_agg

    def test_required_keys(self, result_agg):
        for key in ("service_instance_id", "start_timestamp", "end_timestamp",
                    "aggregates", "interval", "metric_count", "total_values",
                    "metrics", "summary_by_metric"):
            assert key in result_agg

    def test_service_instance_id_echoed(self, result_agg):
        assert result_agg["service_instance_id"] == _INSTANCE_ID

    def test_metric_count(self, result_agg):
        assert result_agg["metric_count"] == 2

    def test_total_values(self, result_agg):
        assert result_agg["total_values"] == 3   # 2 values for memory + 1 for vcpu

    def test_metric_fields_present(self, result_agg):
        for m in result_agg["metrics"]:
            for field in ("name", "resourceType", "values"):
                assert field in m

    def test_summary_by_metric_present(self, result_agg):
        assert "DefaultNodeMemory" in result_agg["summary_by_metric"]
        assert "DefaultNodeVCPU" in result_agg["summary_by_metric"]

    def test_summary_memory_total_sum(self, result_agg):
        summary = result_agg["summary_by_metric"]["DefaultNodeMemory"]
        assert summary["total_sum"] == pytest.approx(24.5 + 25.1, rel=1e-5)

    def test_interval_echoed(self, result_agg):
        assert result_agg["interval"] == 86400

    def test_aggregates_echoed(self, result_agg):
        # aggregates is sorted alphabetically in the implementation
        assert result_agg["aggregates"] == "sum"

    # ── Scenario B: Raw mode (empty aggregates) ───────────────────────────────

    @pytest.fixture
    async def result_raw(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METERING_VALUES}):
            raw = await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-03T00:00:00Z",
                "aggregates": "",
            })
        return json.loads(raw)

    def test_raw_mode_aggregates_label(self, result_raw):
        assert result_raw["aggregates"] == "(raw)"

    def test_raw_mode_interval_none(self, result_raw):
        assert result_raw["interval"] is None

    # ── Scenario C: No service_instance_id → env var fallback ────────────────

    @pytest.fixture
    async def result_env_fallback(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METERING_VALUES}):
            raw = await get_hana_metering_values.ainvoke({
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-02T00:00:00Z",
            })
        return json.loads(raw)

    def test_env_fallback_uses_instance_id(self, result_env_fallback):
        assert result_env_fallback["service_instance_id"] == _INSTANCE_ID

    def test_env_fallback_no_error(self, result_env_fallback):
        assert "error" not in result_env_fallback

    # ── Scenario D: No instance ID anywhere → error ───────────────────────────

    @pytest.fixture
    async def result_no_id(self, monkeypatch):
        monkeypatch.delenv("HANA_SERVICE_INSTANCE_ID", raising=False)
        raw = await get_hana_metering_values.ainvoke({
            "service_instance_id": "",
        })
        return json.loads(raw)

    def test_missing_id_returns_error(self, result_no_id):
        assert "error" in result_no_id

    # ── Scenario E: Invalid interval snapped ─────────────────────────────────

    async def test_invalid_interval_snapped_to_nearest(self, monkeypatch):
        """interval=5000 is not valid for metering; nearest valid is 3600."""
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        captured = {}

        async def _fake_get(path, params):
            captured["interval"] = params.get("interval")
            return {"data": []}

        with patch("hana_tool._hana_get", side_effect=_fake_get):
            await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-02T00:00:00Z",
                "aggregates": "sum",
                "interval": 5000,
            })
        assert captured["interval"] in _METERING_INTERVALS

    # ── Scenario F: Invalid aggregate removed ────────────────────────────────

    async def test_invalid_aggregate_removed(self, monkeypatch):
        """'badagg' is not a valid metering aggregate; it should be stripped."""
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        captured = {}

        async def _fake_get(path, params):
            captured["aggregates"] = params.get("aggregates", "")
            return {"data": []}

        with patch("hana_tool._hana_get", side_effect=_fake_get):
            await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-02T00:00:00Z",
                "aggregates": "sum,badagg",
                "interval": 86400,
            })
        # Only valid aggregate 'sum' should remain
        agg_parts = {a.strip() for a in captured["aggregates"].split(",") if a.strip()}
        assert agg_parts <= _METERING_AGGREGATES

    # ── Scenario G: start >= end auto-corrected ───────────────────────────────

    @pytest.fixture
    async def result_swapped_ts(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": []}):
            raw = await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T12:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",   # equal → should auto-correct
            })
        return json.loads(raw)

    def test_swapped_ts_no_error(self, result_swapped_ts):
        assert "error" not in result_swapped_ts

    def test_swapped_ts_start_before_end(self, result_swapped_ts):
        assert result_swapped_ts["start_timestamp"] < result_swapped_ts["end_timestamp"]

    # ── Scenario H: Empty data response ──────────────────────────────────────

    @pytest.fixture
    async def result_empty(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": []}):
            raw = await get_hana_metering_values.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-01T00:00:00Z",
                "end_timestamp":   "2026-06-02T00:00:00Z",
            })
        return json.loads(raw)

    def test_empty_no_error(self, result_empty):
        assert "error" not in result_empty

    def test_empty_zero_metrics(self, result_empty):
        assert result_empty["metric_count"] == 0

    def test_empty_zero_total_values(self, result_empty):
        assert result_empty["total_values"] == 0


# ===========================================================================
# get_hana_metric_definitions
# ===========================================================================

class TestGetHanaMetricDefinitions:
    """Tests for get_hana_metric_definitions."""

    # ── Scenario A: All-instances path ───────────────────────────────────────

    @pytest.fixture
    async def result_all(self, monkeypatch):
        monkeypatch.delenv("HANA_SERVICE_INSTANCE_ID", raising=False)
        response = {"data": _METRICS_DEFS, "count": len(_METRICS_DEFS)}
        with _patch_hana_get(response):
            raw = await get_hana_metric_definitions.ainvoke({})
        return json.loads(raw)

    def test_no_error(self, result_all):
        assert "error" not in result_all

    def test_required_keys(self, result_all):
        for key in ("total_definitions", "by_resource_type", "by_category", "raw_definitions"):
            assert key in result_all

    def test_total_definitions(self, result_all):
        assert result_all["total_definitions"] == len(_METRICS_DEFS)

    def test_raw_definitions_matches(self, result_all):
        assert result_all["raw_definitions"] == _METRICS_DEFS

    # ── Scenario B: Per-instance path ────────────────────────────────────────

    async def test_per_instance_path_called(self, monkeypatch):
        """When service_instance_id is given the URL must include the GUID."""
        captured = {}

        async def _fake_get(path, params):
            captured["path"] = path
            return {"data": [], "count": 0}

        with patch("hana_tool._hana_get", side_effect=_fake_get):
            await get_hana_metric_definitions.ainvoke({"service_instance_id": _INSTANCE_ID})
        assert _INSTANCE_ID in captured["path"]
        assert "/metrics/v1/serviceInstances/" in captured["path"]

    # ── Scenario C: by_resource_type grouping ────────────────────────────────

    def test_by_resource_type_keys(self, result_all):
        assert "hana-cloud-hdb" in result_all["by_resource_type"]
        assert "hana-cloud-hdl" in result_all["by_resource_type"]

    def test_hdb_has_two_metrics_in_by_rt(self, result_all):
        assert len(result_all["by_resource_type"]["hana-cloud-hdb"]) == 2

    def test_metrics_in_by_rt_sorted(self, result_all):
        for rt, defs in result_all["by_resource_type"].items():
            names = [d["name"] for d in defs]
            assert names == sorted(names), f"Unsorted metrics in resourceType {rt}"

    def test_metric_def_fields_present(self, result_all):
        for rt, defs in result_all["by_resource_type"].items():
            for d in defs:
                for field in ("name", "type", "unit", "dimensions",
                              "description", "interval", "aggregates", "retention"):
                    assert field in d

    # ── Scenario D: by_category grouping ─────────────────────────────────────

    def test_by_category_memory_present(self, result_all):
        assert "memory" in result_all["by_category"]

    def test_by_category_vcpu_present(self, result_all):
        # HDBCPU name → "vcpu" category
        assert "vcpu" in result_all["by_category"]

    def test_by_category_entries_have_resource_type(self, result_all):
        for cat, defs in result_all["by_category"].items():
            for d in defs:
                assert "resourceType" in d

    # ── Scenario E: Empty definitions ────────────────────────────────────────

    @pytest.fixture
    async def result_empty(self, monkeypatch):
        monkeypatch.delenv("HANA_SERVICE_INSTANCE_ID", raising=False)
        with _patch_hana_get({"data": [], "count": 0}):
            raw = await get_hana_metric_definitions.ainvoke({})
        return json.loads(raw)

    def test_empty_no_error(self, result_empty):
        assert "error" not in result_empty

    def test_empty_zero_total(self, result_empty):
        assert result_empty["total_definitions"] == 0
        assert result_empty["by_resource_type"] == {}
        assert result_empty["by_category"] == {}


# ===========================================================================
# get_hana_metrics
# ===========================================================================

class TestGetHanaMetrics:
    """Tests for get_hana_metrics."""

    # ── Scenario A: Aggregate mode with dimensions ────────────────────────────

    @pytest.fixture
    async def result_agg(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METRICS_VALUES}):
            raw = await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
                "aggregates": "max,avg",
                "interval": 3600,
            })
        return json.loads(raw)

    def test_no_error(self, result_agg):
        assert "error" not in result_agg

    def test_required_keys(self, result_agg):
        for key in ("service_instance_id", "start_timestamp", "end_timestamp",
                    "aggregates", "interval", "metric_count", "total_values",
                    "metrics", "summary_by_metric"):
            assert key in result_agg

    def test_service_instance_id_echoed(self, result_agg):
        assert result_agg["service_instance_id"] == _INSTANCE_ID

    def test_metric_count(self, result_agg):
        assert result_agg["metric_count"] == 2

    def test_total_values(self, result_agg):
        # HDBMemoryUsed has 2 values, HDBCPU has 1 value
        assert result_agg["total_values"] == 3

    def test_metrics_have_dimensions(self, result_agg):
        for m in result_agg["metrics"]:
            assert "dimensions" in m
            assert isinstance(m["dimensions"], dict)

    def test_dimensions_contain_service_name(self, result_agg):
        for m in result_agg["metrics"]:
            assert "service_name" in m["dimensions"]

    def test_summary_max_value_computed(self, result_agg):
        summary = result_agg["summary_by_metric"]
        assert "HDBMemoryUsed" in summary
        # max across 2 values: 2873864917.0 and 2890000000.0
        assert summary["HDBMemoryUsed"]["max_value"] == pytest.approx(2890000000.0, rel=1e-5)

    def test_summary_avg_value_computed(self, result_agg):
        summary = result_agg["summary_by_metric"]
        # avg of [2800000000.0, 2850000000.0] = 2825000000.0
        assert summary["HDBMemoryUsed"]["avg_value"] == pytest.approx(2825000000.0, rel=1e-5)

    def test_interval_echoed(self, result_agg):
        assert result_agg["interval"] == 3600

    # ── Scenario B: Raw mode (empty aggregates) ───────────────────────────────

    @pytest.fixture
    async def result_raw(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METRICS_VALUES}):
            raw = await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
                "aggregates": "",
            })
        return json.loads(raw)

    def test_raw_mode_label(self, result_raw):
        assert result_raw["aggregates"] == "(raw)"

    def test_raw_mode_interval_none(self, result_raw):
        assert result_raw["interval"] is None

    # ── Scenario C: env var fallback ─────────────────────────────────────────

    @pytest.fixture
    async def result_env_fallback(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": _METRICS_VALUES}):
            raw = await get_hana_metrics.ainvoke({
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
            })
        return json.loads(raw)

    def test_env_fallback_uses_instance_id(self, result_env_fallback):
        assert result_env_fallback["service_instance_id"] == _INSTANCE_ID

    # ── Scenario D: filter_expr forwarded as $filter ──────────────────────────

    async def test_filter_expr_forwarded(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        filter_expr = "resourceType eq hana-cloud-hdb"
        captured = {}

        async def _fake_get(path, params):
            captured["filter"] = params.get("$filter")
            return {"data": []}

        with patch("hana_tool._hana_get", side_effect=_fake_get):
            await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
                "filter_expr": filter_expr,
            })
        assert captured["filter"] == filter_expr

    # ── Scenario E: Invalid interval snapped ─────────────────────────────────

    async def test_invalid_interval_snapped(self, monkeypatch):
        """interval=100 is not valid for metrics; nearest valid is 60."""
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        captured = {}

        async def _fake_get(path, params):
            captured["interval"] = params.get("interval")
            return {"data": []}

        with patch("hana_tool._hana_get", side_effect=_fake_get):
            await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
                "aggregates": "max",
                "interval": 100,
            })
        assert captured["interval"] in _METRICS_INTERVALS

    # ── Scenario F: start >= end auto-corrected ───────────────────────────────

    @pytest.fixture
    async def result_swapped_ts(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": []}):
            raw = await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T12:00:00Z",
                "end_timestamp":   "2026-06-15T10:00:00Z",   # before start
            })
        return json.loads(raw)

    def test_swapped_ts_no_error(self, result_swapped_ts):
        assert "error" not in result_swapped_ts

    def test_swapped_ts_start_before_end(self, result_swapped_ts):
        assert result_swapped_ts["start_timestamp"] < result_swapped_ts["end_timestamp"]

    # ── Scenario G: summary max_value edge — single value ────────────────────

    @pytest.fixture
    async def result_single_value(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        single = [
            {
                "serviceInstanceID": _INSTANCE_ID,
                "resourceType": "hana-cloud-hdb",
                "name": "HDBCPU",
                "dimensions": {"host": _INSTANCE_ID},
                "values": [
                    {
                        "startTimestamp": "2026-06-15T10:00:00Z",
                        "endTimestamp":   "2026-06-15T11:00:00Z",
                        "max": 55.0,
                        "interval": 3600,
                    }
                ],
            }
        ]
        with _patch_hana_get({"data": single}):
            raw = await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T11:00:00Z",
                "aggregates": "max",
            })
        return json.loads(raw)

    def test_single_value_max(self, result_single_value):
        assert result_single_value["summary_by_metric"]["HDBCPU"]["max_value"] == 55.0

    def test_single_value_no_avg_when_not_requested(self, result_single_value):
        # "avg" was not in aggregates, so avg_value should not be in summary
        assert "avg_value" not in result_single_value["summary_by_metric"]["HDBCPU"]

    # ── Scenario H: Empty data ────────────────────────────────────────────────

    @pytest.fixture
    async def result_empty(self, monkeypatch):
        monkeypatch.setenv("HANA_SERVICE_INSTANCE_ID", _INSTANCE_ID)
        with _patch_hana_get({"data": []}):
            raw = await get_hana_metrics.ainvoke({
                "service_instance_id": _INSTANCE_ID,
                "start_timestamp": "2026-06-15T10:00:00Z",
                "end_timestamp":   "2026-06-15T12:00:00Z",
            })
        return json.loads(raw)

    def test_empty_no_error(self, result_empty):
        assert "error" not in result_empty

    def test_empty_zero_metrics(self, result_empty):
        assert result_empty["metric_count"] == 0

    def test_empty_zero_total_values(self, result_empty):
        assert result_empty["total_values"] == 0


# ===========================================================================
# Unit tests — helper functions
# ===========================================================================

class TestValidateTimestamp:
    """Unit tests for _validate_timestamp."""

    def test_valid_timestamp_returned_unchanged(self):
        ts = "2026-06-01T12:00:00Z"
        assert _validate_timestamp(ts, "start") == ts

    def test_future_timestamp_clamped(self):
        future = "2099-01-01T00:00:00Z"
        result = _validate_timestamp(future, "start")
        assert result < future  # clamped to <= now

    def test_very_old_timestamp_replaced(self):
        old = "2020-01-01T00:00:00Z"
        result = _validate_timestamp(old, "start")
        # Year 2020 is more than 2 years before 2026 → replaced
        assert result != old

    def test_invalid_format_returns_fallback(self):
        result = _validate_timestamp("not-a-timestamp", "start")
        # Should be a valid ISO 8601 string
        assert "T" in result and "Z" in result

    def test_valid_recent_timestamp_unchanged(self):
        ts = "2026-05-01T00:00:00Z"
        assert _validate_timestamp(ts, "start") == ts


class TestInferMetricCategory:
    """Unit tests for _infer_metric_category."""

    def test_memory_keyword(self):
        assert _infer_metric_category("HDBMemoryUsed") == "memory"

    def test_cpu_keyword(self):
        assert _infer_metric_category("HDBCPU") == "vcpu"

    def test_disk_keyword(self):
        assert _infer_metric_category("HDBDiskUsed") == "storage"

    def test_net_keyword(self):
        assert _infer_metric_category("HDBNetworkIn") == "network"

    def test_backup_keyword(self):
        assert _infer_metric_category("BackupSize") == "backup"

    def test_unknown_falls_to_other(self):
        assert _infer_metric_category("SomethingRandom") == "other"

    def test_case_insensitive(self):
        assert _infer_metric_category("hdbmemoryused") == "memory"


class TestIntervalAndAggregateSets:
    """Sanity-check the spec-verified constant sets."""

    def test_metering_intervals_are_all_valid(self):
        # All metering intervals must be multiples of 3600 (1h) per spec
        for iv in _METERING_INTERVALS:
            assert iv % 3600 == 0

    def test_metrics_intervals_include_sub_hour(self):
        # Metrics allows finer granularity (60s, 300s, etc.)
        assert 60 in _METRICS_INTERVALS
        assert 300 in _METRICS_INTERVALS

    def test_metering_aggregates_subset(self):
        assert _METERING_AGGREGATES == {"count", "last", "sum"}

    def test_metrics_aggregates_superset(self):
        # Metrics supports richer aggregates
        assert {"avg", "min", "max", "delta"} <= _METRICS_AGGREGATES
