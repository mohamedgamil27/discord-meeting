from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LOGGER = logging.getLogger("egx30.collector")
SOURCE_URL = "https://www.egx.com.eg/ar/Indices.aspx"
SOURCE_NAME = "EGX"
INDEX_NAME = "EGX30"

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "Date": ("date", "التاريخ"),
    "Open": ("open", "opening", "الافتتاح", "افتتاح"),
    "High": ("high", "highest", "اعلى", "الأعلى"),
    "Low": ("low", "lowest", "ادنى", "الأدنى"),
    "Close": ("close", "closing", "اغلاق", "الإغلاق"),
    "ChangePercent": ("change%", "change", "نسبة التغير", "التغير%", "التغير"),
    "YTDPercent": (
        "ytd%",
        "year to date",
        "منذ بداية العام",
        "التغير منذ بداية العام",
    ),
    "IndexValueUSD": (
        "index value usd",
        "index usd",
        "قيمة المؤشر بالدولار",
        "قيمة المؤشر (دولار)",
    ),
    "ChangePercentUSD": (
        "change% usd",
        "usd change%",
        "نسبة التغير بالدولار",
    ),
    "TotalTradedValueEGP": (
        "total traded value",
        "traded value",
        "اجمالي قيمة التداول",
        "إجمالي قيمة التداول",
    ),
    "TotalTradedValueMarketPercent": (
        "traded value market%",
        "market share value",
        "نسبة من السوق قيمة التداول",
    ),
    "TotalVolume": (
        "total volume",
        "volume",
        "اجمالي حجم التداول",
        "إجمالي حجم التداول",
    ),
    "TotalVolumeMarketPercent": (
        "volume market%",
        "market share volume",
        "نسبة من السوق حجم التداول",
    ),
    "TradesCount": (
        "trades count",
        "number of trades",
        "عدد العمليات",
        "عدد الصفقات",
    ),
    "TradesMarketPercent": (
        "trades market%",
        "market share trades",
        "نسبة من السوق عدد العمليات",
    ),
    "MarketCapEGP": (
        "market cap",
        "market capitalization",
        "راس المال السوقي",
        "رأس المال السوقي",
    ),
    "MarketCapMarketPercent": (
        "market cap market%",
        "market share cap",
        "نسبة من السوق رأس المال",
    ),
}

NUMERIC_FIELDS = [
    "Open",
    "High",
    "Low",
    "Close",
    "ChangePercent",
    "YTDPercent",
    "IndexValueUSD",
    "ChangePercentUSD",
    "TotalTradedValueEGP",
    "TotalTradedValueMarketPercent",
    "TotalVolume",
    "TotalVolumeMarketPercent",
    "TradesCount",
    "TradesMarketPercent",
    "MarketCapEGP",
    "MarketCapMarketPercent",
]


@dataclass
class AttemptContext:
    attempt: int
    diagnostics_dir: Path


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def normalize_label(value: str) -> str:
    normalized = value.translate(ARABIC_DIGITS).lower().strip()
    normalized = re.sub(r"[\s\u00a0]+", " ", normalized)
    normalized = re.sub(r"[:：\-]+", "", normalized)
    return normalized.strip()


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None

    raw = str(value).translate(ARABIC_DIGITS).strip()
    if not raw:
        return None

    raw = raw.replace("%", "").replace("،", ",")
    raw = re.sub(r"[^0-9,\.\-+]", "", raw)
    if not raw:
        return None

    if raw.count(",") and raw.count("."):
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(",") and not raw.count("."):
        if raw.count(",") > 1:
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", ".")

    try:
        num = float(raw)
    except ValueError:
        return None

    return num if math.isfinite(num) else None


def parse_session_date(value: str) -> date | None:
    if not value:
        return None
    cleaned = value.translate(ARABIC_DIGITS).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    return None


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def write_csv_atomic(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", delete=False, dir=path.parent
    ) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        temp_name = tmp.name
    os.replace(temp_name, path)


def collect_page_payload() -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(2_000)

        page.evaluate(
            """
            () => {
              const selectorCandidates = [
                'select[id*=Index i]',
                'select[id*=indices i]',
                'select[name*=Index i]',
                'select[name*=indices i]',
                '#ddlIndices',
                '#ContentPlaceHolder1_ddlIndices'
              ];
              let target = null;
              for (const selector of selectorCandidates) {
                const el = document.querySelector(selector);
                if (el) {
                  target = el;
                  break;
                }
              }
              if (!target) return;
              const options = Array.from(target.options || []);
              const chosen = options.find((o) => /egx\s*30/i.test((o.textContent || '') + ' ' + (o.value || '')));
              if (chosen) {
                target.value = chosen.value;
                target.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }
            """
        )
        page.wait_for_timeout(2_000)

        payload = page.evaluate(
            """
            () => {
              const norm = (v) => (v || '').replace(/\u00a0/g, ' ').trim();
              const pairs = [];

              const pushPair = (k, v) => {
                const key = norm(k);
                const val = norm(v);
                if (key && val) pairs.push([key, val]);
              };

              for (const row of document.querySelectorAll('tr')) {
                const cells = Array.from(row.querySelectorAll('th,td')).map((c) => norm(c.textContent));
                if (cells.length >= 2) pushPair(cells[0], cells[1]);
              }

              for (const dl of document.querySelectorAll('dl')) {
                const dts = Array.from(dl.querySelectorAll('dt'));
                const dds = Array.from(dl.querySelectorAll('dd'));
                for (let i = 0; i < Math.min(dts.length, dds.length); i += 1) {
                  pushPair(dts[i].textContent, dds[i].textContent);
                }
              }

              const candidateSelectors = [
                '.index-summary li',
                '.summary li',
                '.stats li',
                '.market-summary li',
                '[class*=summary] li',
                '[class*=stat] li',
                '.card-body div',
                '.row .col, .row .col-md-3, .row .col-sm-3'
              ];

              for (const selector of candidateSelectors) {
                for (const node of document.querySelectorAll(selector)) {
                  const spans = Array.from(node.querySelectorAll('span, strong, b'));
                  if (spans.length >= 2) {
                    pushPair(spans[0].textContent, spans[1].textContent);
                  } else {
                    const text = norm(node.textContent);
                    const m = text.match(/^([^:]+):\s*(.+)$/);
                    if (m) pushPair(m[1], m[2]);
                  }
                }
              }

              const chartSeries = [];
              const categories = [];
              if (window.Highcharts && Array.isArray(window.Highcharts.charts)) {
                for (const chart of window.Highcharts.charts) {
                  if (!chart) continue;
                  const xCategories = chart.xAxis && chart.xAxis[0] && Array.isArray(chart.xAxis[0].categories)
                    ? chart.xAxis[0].categories
                    : [];
                  categories.push(...xCategories);
                  for (const series of chart.series || []) {
                    if (!series || !Array.isArray(series.data) || !series.data.length) continue;
                    chartSeries.push({
                      name: series.name || '',
                      points: series.data.map((p) => ({
                        x: p && p.x !== undefined ? p.x : null,
                        y: p && p.y !== undefined ? p.y : null,
                        name: p && p.name ? p.name : null,
                        category: p && p.category ? p.category : null,
                      }))
                    });
                  }
                }
              }

              return {
                pairs,
                chartSeries,
                categories,
                html: document.documentElement.outerHTML,
                title: document.title || ''
              };
            }
            """
        )
        screenshot_bytes = page.screenshot(full_page=True)
        browser.close()

    payload["screenshot"] = screenshot_bytes
    return payload


def save_diagnostics(
    diagnostics_dir: Path,
    attempt: int,
    error_message: str,
    html: str | None,
    screenshot: bytes | None,
) -> None:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    prefix = diagnostics_dir / f"attempt_{attempt}_{stamp}"
    (prefix.with_suffix(".log")).write_text(error_message + "\n", encoding="utf-8")
    if html:
        (prefix.with_suffix(".html")).write_text(html, encoding="utf-8")
    if screenshot:
        prefix.with_suffix(".png").write_bytes(screenshot)


def pick_value_from_pairs(pairs: list[tuple[str, str]], field_name: str) -> str | None:
    aliases = [normalize_label(alias) for alias in FIELD_ALIASES[field_name]]
    for key, value in pairs:
        nk = normalize_label(key)
        if any(alias in nk for alias in aliases):
            return value
    return None


def extract_daily_summary(raw_pairs: list[list[str]], now_iso: str) -> dict[str, Any]:
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        if len(pair) >= 2:
            pairs.append((pair[0], pair[1]))

    result: dict[str, Any] = {}
    raw_date = pick_value_from_pairs(pairs, "Date")
    parsed_date = parse_session_date(raw_date or "") if raw_date else None
    if parsed_date is None:
        parsed_date = datetime.utcnow().date()
    result["Date"] = parsed_date.isoformat()

    for field in NUMERIC_FIELDS:
        value = pick_value_from_pairs(pairs, field)
        number = parse_number(value)
        if number is None:
            raise ValueError(f"Missing or invalid numeric value for '{field}'")
        result[field] = number

    validate_daily_summary(result)
    result["Source"] = SOURCE_URL
    result["CollectedAt"] = now_iso
    return result


def extract_chart_points(payload: dict[str, Any], session_date: date) -> list[dict[str, Any]]:
    chart_series = payload.get("chartSeries") or []
    categories = payload.get("categories") or []

    best_series = None
    best_count = -1
    for series in chart_series:
        points = series.get("points") or []
        valid_count = sum(1 for p in points if parse_number(p.get("y")) is not None)
        if valid_count > best_count:
            best_count = valid_count
            best_series = series

    if not best_series:
        return []

    deduped: dict[str, dict[str, Any]] = {}
    for idx, point in enumerate(best_series.get("points") or []):
        value = parse_number(point.get("y"))
        if value is None:
            continue

        original_time = point.get("category") or point.get("name")
        ts = normalize_point_timestamp(point.get("x"), original_time, session_date, idx, categories)
        if ts is None:
            continue

        deduped[ts.isoformat()] = {
            "Date": session_date.isoformat(),
            "OriginalChartTime": original_time if original_time else ts.strftime("%H:%M"),
            "NormalizedTimestamp": ts.isoformat(),
            "Value": value,
            "Source": SOURCE_URL,
        }

    points = sorted(deduped.values(), key=lambda item: item["NormalizedTimestamp"])
    return points


def normalize_point_timestamp(
    x_value: Any,
    original_time: str | None,
    session_date: date,
    idx: int,
    categories: list[Any],
) -> datetime | None:
    if isinstance(x_value, (int, float)):
        try:
            if x_value > 1e11:
                return datetime.utcfromtimestamp(float(x_value) / 1000.0)
            if x_value > 1e9:
                return datetime.utcfromtimestamp(float(x_value))
        except (OverflowError, OSError, ValueError):
            pass

    time_text = original_time or (str(categories[idx]) if idx < len(categories) else "")
    time_text = str(time_text).translate(ARABIC_DIGITS).strip()
    match = re.search(r"(\d{1,2}):(\d{2})", time_text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return datetime.combine(session_date, datetime.min.time()).replace(hour=hour, minute=minute)


def validate_daily_summary(summary: dict[str, Any]) -> None:
    high = summary["High"]
    low = summary["Low"]
    open_value = summary["Open"]
    close = summary["Close"]

    if not (high >= open_value):
        raise ValueError("Validation failed: High must be >= Open")
    if not (high >= close):
        raise ValueError("Validation failed: High must be >= Close")
    if not (low <= open_value):
        raise ValueError("Validation failed: Low must be <= Open")
    if not (low <= close):
        raise ValueError("Validation failed: Low must be <= Close")
    if not (high >= low):
        raise ValueError("Validation failed: High must be >= Low")


def derive_features(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        raise ValueError("No chart points found")

    feature_rows: list[dict[str, Any]] = []
    values_window: list[float] = []
    running_high = -math.inf
    running_low = math.inf

    session_open = points[0]["Value"]
    open_time = datetime.fromisoformat(points[0]["NormalizedTimestamp"])
    previous_value: float | None = None
    previous_time: datetime | None = None

    for point in points:
        ts = datetime.fromisoformat(point["NormalizedTimestamp"])
        value = point["Value"]

        point_change = value - previous_value if previous_value is not None else 0.0
        return_5m = (
            (point_change / previous_value) * 100.0 if previous_value not in (None, 0) else 0.0
        )
        if point_change > 0:
            direction = "up"
        elif point_change < 0:
            direction = "down"
        else:
            direction = "flat"

        running_high = max(running_high, value)
        running_low = min(running_low, value)

        range_span = running_high - running_low
        position_in_range = (
            ((value - running_low) / range_span) * 100.0 if range_span > 0 else 0.0
        )

        values_window.append(value)
        ma3 = statistics.fmean(values_window[-3:])
        ma6 = statistics.fmean(values_window[-6:])
        std3 = statistics.pstdev(values_window[-3:]) if len(values_window[-3:]) > 1 else 0.0
        std6 = statistics.pstdev(values_window[-6:]) if len(values_window[-6:]) > 1 else 0.0

        minutes_from_open = int((ts - open_time).total_seconds() // 60)
        interval_minutes = (
            int((ts - previous_time).total_seconds() // 60) if previous_time else 0
        )
        quality = "ok" if interval_minutes in (0, 5) else f"irregular_interval_{interval_minutes}"

        feature_rows.append(
            {
                **point,
                "PreviousValue": previous_value,
                "PointChange": point_change,
                "Return5mPercent": return_5m,
                "Direction": direction,
                "SessionOpen": session_open,
                "ChangeFromOpen": value - session_open,
                "ChangeFromOpenPercent": ((value - session_open) / session_open) * 100.0
                if session_open != 0
                else 0.0,
                "RunningHigh": running_high,
                "RunningLow": running_low,
                "DistanceFromRunningHigh": value - running_high,
                "DistanceFromRunningLow": value - running_low,
                "PositionInObservedRangePercent": position_in_range,
                "MovingAverage3": ma3,
                "MovingAverage6": ma6,
                "RollingStd3": std3,
                "RollingStd6": std6,
                "MinutesFromOpen": minutes_from_open,
                "Hour": ts.hour,
                "Minute": ts.minute,
                "IntervalMinutes": interval_minutes,
                "DataQualityStatus": quality,
            }
        )

        previous_value = value
        previous_time = ts

    return feature_rows


def save_outputs(
    output_root: Path,
    daily_summary: dict[str, Any],
    points: list[dict[str, Any]],
    features: list[dict[str, Any]],
) -> list[Path]:
    session_date = datetime.strptime(daily_summary["Date"], "%Y-%m-%d").date()
    target_dir = output_root / f"{session_date.year:04d}" / f"{session_date.month:02d}" / f"{session_date.day:02d}"
    date_str = session_date.isoformat()

    daily_csv = target_dir / f"egx30_daily_{date_str}.csv"
    daily_json = target_dir / f"egx30_daily_{date_str}.json"
    points_csv = target_dir / f"egx30_5minute_{date_str}.csv"
    points_json = target_dir / f"egx30_5minute_{date_str}.json"
    features_csv = target_dir / f"egx30_features_{date_str}.csv"
    features_json = target_dir / f"egx30_features_{date_str}.json"

    write_csv_atomic(daily_csv, [daily_summary], list(daily_summary.keys()))
    write_json_atomic(daily_json, daily_summary)

    write_csv_atomic(points_csv, points, list(points[0].keys()))
    write_json_atomic(points_json, points)

    write_csv_atomic(features_csv, features, list(features[0].keys()))
    write_json_atomic(features_json, features)

    return [daily_csv, daily_json, points_csv, points_json, features_csv, features_json]


def collect_with_retries(output_root: Path, diagnostics_dir: Path, retries: int, delay_seconds: float) -> list[Path]:
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            payload = collect_page_payload()
            daily_summary = extract_daily_summary(payload.get("pairs") or [], now_iso)
            session_date = datetime.strptime(daily_summary["Date"], "%Y-%m-%d").date()
            points = extract_chart_points(payload, session_date)
            if not points:
                raise ValueError("No chart points found")
            features = derive_features(points)
            paths = save_outputs(output_root, daily_summary, points, features)
            LOGGER.info("Collection succeeded with %d chart points.", len(points))
            return paths
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            LOGGER.exception("Attempt %d/%d failed.", attempt, retries)
            html = None
            screenshot = None
            if isinstance(exc, (PlaywrightError, PlaywrightTimeoutError)):
                html = None
            if "payload" in locals() and isinstance(payload, dict):
                html = payload.get("html")
                screenshot = payload.get("screenshot")
            save_diagnostics(
                diagnostics_dir=diagnostics_dir,
                attempt=attempt,
                error_message=str(exc),
                html=html,
                screenshot=screenshot,
            )
            if attempt < retries:
                time.sleep(delay_seconds)

    raise RuntimeError(f"Failed after {retries} attempts") from last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect EGX30 daily and 5-minute market data")
    parser.add_argument(
        "--output-root",
        default="data",
        help="Root directory for output files (default: data)",
    )
    parser.add_argument(
        "--diagnostics-dir",
        default="diagnostics",
        help="Directory for failure diagnostics (default: diagnostics)",
    )
    parser.add_argument("--retries", type=int, default=3, help="Retry attempts")
    parser.add_argument("--retry-delay", type=float, default=15.0, help="Retry delay seconds")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    output_root = Path(args.output_root).resolve()
    diagnostics_dir = Path(args.diagnostics_dir).resolve()

    try:
        written = collect_with_retries(
            output_root=output_root,
            diagnostics_dir=diagnostics_dir,
            retries=max(1, args.retries),
            delay_seconds=max(1.0, args.retry_delay),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Collection failed: %s", exc)
        return 1

    LOGGER.info("Wrote %d files:", len(written))
    for path in written:
        LOGGER.info("- %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
