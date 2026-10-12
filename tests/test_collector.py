from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collect_egx30 import (  # noqa: E402
    build_chart_points,
    build_features,
    build_output_paths,
    parse_egx_date,
    parse_number,
    validate_ohlc,
)


def test_parse_number_handles_arabic_and_percent() -> None:
    assert parse_number("١٢٬٣٤٥.٦٧") == pytest.approx(12345.67)
    assert parse_number("-2.5%") == pytest.approx(-2.5)


def test_parse_egx_date_formats() -> None:
    assert parse_egx_date("12/10/2026") == date(2026, 10, 12)
    assert parse_egx_date("2026-10-12") == date(2026, 10, 12)


def test_chart_points_normalization_filter_and_dedup() -> None:
    points = build_chart_points(
        raw_points=[
            "10:00 ص - 30000",
            "10:05 ص - 30010",
            "10:05 ص - 30011",
            "2:35 م - 30200",
        ],
        session_date=date(2026, 10, 12),
        cutoff_hhmm="14:30",
        collected_at="2026-10-12T13:00:00Z",
    )

    assert [p["Timestamp"] for p in points] == [
        "2026-10-12T10:00:00",
        "2026-10-12T10:05:00",
    ]
    assert points[1]["Value"] == pytest.approx(30010.0)


def test_validate_ohlc_rules() -> None:
    valid = {"Open": 100.0, "High": 120.0, "Low": 95.0, "Close": 110.0}
    validate_ohlc(valid)

    invalid = {"Open": 100.0, "High": 99.0, "Low": 90.0, "Close": 95.0}
    with pytest.raises(Exception):
        validate_ohlc(invalid)


def test_feature_calculations_and_population_std() -> None:
    points = [
        {
            "Timestamp": "2026-10-12T10:00:00",
            "RawChartTime": "10:00 ص",
            "NormalizedTime24h": "10:00",
            "Value": 100.0,
            "IntervalMinutes": 5,
            "Source": "x",
            "CollectedAt": "x",
        },
        {
            "Timestamp": "2026-10-12T10:05:00",
            "RawChartTime": "10:05 ص",
            "NormalizedTime24h": "10:05",
            "Value": 102.0,
            "IntervalMinutes": 5,
            "Source": "x",
            "CollectedAt": "x",
        },
        {
            "Timestamp": "2026-10-12T10:10:00",
            "RawChartTime": "10:10 ص",
            "NormalizedTime24h": "10:10",
            "Value": 101.0,
            "IntervalMinutes": 5,
            "Source": "x",
            "CollectedAt": "x",
        },
    ]

    features = build_features(points)
    assert features[0]["Direction"] == "OPEN"
    assert features[1]["Direction"] == "UP"
    assert features[2]["Direction"] == "DOWN"
    assert features[2]["MovingAverage3"] == pytest.approx((100 + 102 + 101) / 3)
    assert features[2]["RollingStd3"] == pytest.approx(0.81649658, abs=1e-6)


def test_output_paths_structure() -> None:
    paths = build_output_paths(Path("data"), date(2026, 10, 12))
    assert paths["daily_csv"].as_posix().endswith("data/2026/10/12/egx30_daily_2026-10-12.csv")
    assert paths["features_json"].as_posix().endswith("data/2026/10/12/egx30_features_2026-10-12.json")
