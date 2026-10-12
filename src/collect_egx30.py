from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://www.egx.com.eg/ar/Indices.aspx"
SOURCE_NAME = "EGX Arabic Indices Page"
TIMEZONE_NAME = "Africa/Cairo"
INTERVAL_MINUTES = 5
DEFAULT_MARKET_CLOSE = "14:30"

DAILY_FIELD_ORDER = [
    "Date",
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
    "Source",
    "CollectedAt",
]

POINT_FIELD_ORDER = [
    "Timestamp",
    "RawChartTime",
    "NormalizedTime24h",
    "Value",
    "IntervalMinutes",
    "Source",
    "CollectedAt",
]

FEATURE_FIELD_ORDER = [
    "Timestamp",
    "RawChartTime",
    "NormalizedTime24h",
    "Value",
    "PreviousValue",
    "PointChange",
    "Return5mPercent",
    "Direction",
    "SessionOpen",
    "ChangeFromOpen",
    "ChangeFromOpenPercent",
    "RunningHigh",
    "RunningLow",
    "DistanceFromRunningHigh",
    "DistanceFromRunningLow",
    "PositionInObservedRangePercent",
    "MovingAverage3",
    "MovingAverage6",
    "RollingStd3",
    "RollingStd6",
    "MinutesFromOpen",
    "Hour",
    "Minute",
    "IntervalMinutes",
    "Source",
    "DataQualityStatus",
]

REQUIRED_NUMERIC_DAILY_FIELDS = [
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

LOG = logging.getLogger("egx30.collector")


class CollectionError(RuntimeError):
    pass


@dataclass(slots=True)
class RawCapture:
    selected_index: str
    key_value_pairs: list[dict[str, str]]
    chart_raw_points: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect EGX30 daily and intraday data")
    parser.add_argument("--output-root", default="data", help="Output root directory")
    parser.add_argument("--debug-dir", default="debug/egx30", help="Debug diagnostics directory")
    parser.add_argument("--max-attempts", type=int, default=3, help="Maximum retry attempts")
    parser.add_argument("--market-close", default=DEFAULT_MARKET_CLOSE, help="Market close cutoff HH:MM")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _to_ascii_digits(value: str) -> str:
    return value.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))


def _normalize_space(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_number(text: str) -> float:
    normalized = _normalize_space(_to_ascii_digits(text)).replace("٬", ",")
    normalized = normalized.replace("−", "-").replace("%", "")
    normalized = normalized.replace("ج.م", "").replace("$", "")
    normalized = normalized.replace("EGP", "").replace("USD", "")
    normalized = normalized.strip()

    if not normalized:
        raise ValueError("Empty numeric value")

    if "," in normalized and "." in normalized:
        normalized = normalized.replace(",", "")
    elif normalized.count(",") == 1 and normalized.count(".") == 0:
        left, right = normalized.split(",")
        if len(right) <= 2:
            normalized = f"{left}.{right}"
        else:
            normalized = f"{left}{right}"
    else:
        normalized = normalized.replace(",", "")

    value = float(normalized)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite number: {text}")
    return value


def parse_egx_date(text: str) -> date:
    cleaned = _normalize_space(_to_ascii_digits(text)).replace("/", "-")
    cleaned = re.sub(r"[^0-9\-]", "", cleaned)

    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%m-%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.date()
        except ValueError:
            continue

    raise ValueError(f"Unrecognized EGX date: {text}")


def parse_hhmm(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        raise ValueError(f"Invalid HH:MM value: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HH:MM value: {value}")
    return hour, minute


def normalize_chart_time(raw_time: str, previous_minutes: int | None = None) -> tuple[str, int]:
    text = _normalize_space(_to_ascii_digits(raw_time)).lower()
    text = text.replace("a.m", "am").replace("p.m", "pm")
    text = text.replace("ص", "am").replace("م", "pm")

    match = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", text)
    if not match:
        raise ValueError(f"Unable to parse chart time: {raw_time}")

    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3)

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid chart time: {raw_time}")

    candidates: list[int] = []
    if meridiem in {"am", "pm"}:
        h = hour % 12
        if meridiem == "pm":
            h += 12
        candidates = [h * 60 + minute]
    elif hour <= 12:
        candidates = [hour * 60 + minute, ((hour % 12) + 12) * 60 + minute]
    else:
        candidates = [hour * 60 + minute]

    if previous_minutes is None:
        chosen = min(candidates, key=lambda m: abs(m - (10 * 60)))
    else:
        non_decreasing = [m for m in candidates if m >= previous_minutes]
        chosen = min(non_decreasing) if non_decreasing else max(candidates)

    normalized = f"{chosen // 60:02d}:{chosen % 60:02d}"
    return normalized, chosen


def parse_chart_entry(raw_text: str) -> tuple[str, float]:
    normalized = _normalize_space(_to_ascii_digits(raw_text))
    time_match = re.search(r"(\d{1,2}:\d{2}(?:\s*(?:am|pm|ص|م))?)", normalized, re.IGNORECASE)
    if not time_match:
        raise ValueError(f"Chart point missing time: {raw_text}")

    number_candidates = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", normalized)
    if not number_candidates:
        raise ValueError(f"Chart point missing value: {raw_text}")

    chart_value = parse_number(number_candidates[-1])
    return time_match.group(1), chart_value


def validate_ohlc(daily: dict[str, Any]) -> None:
    if not (
        daily["High"] >= daily["Open"]
        and daily["High"] >= daily["Close"]
        and daily["Low"] <= daily["Open"]
        and daily["Low"] <= daily["Close"]
        and daily["High"] >= daily["Low"]
    ):
        raise CollectionError("OHLC validation failed")


def _safe_percent(numerator: float, denominator: float, neutral: float = 0.0) -> float:
    if denominator == 0:
        return neutral
    return (numerator / denominator) * 100.0


def build_features(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[float] = []
    features: list[dict[str, Any]] = []

    for index, point in enumerate(points):
        value = point["Value"]
        values.append(value)

        prev = values[index - 1] if index > 0 else None
        point_change = value - prev if prev is not None else 0.0
        return_percent = _safe_percent(point_change, prev, neutral=0.0) if prev is not None else 0.0

        if index == 0:
            direction = "OPEN"
        elif point_change > 0:
            direction = "UP"
        elif point_change < 0:
            direction = "DOWN"
        else:
            direction = "FLAT"

        session_open = values[0]
        running_high = max(values)
        running_low = min(values)
        observed_range = running_high - running_low

        window3 = values[max(0, index - 2) : index + 1]
        window6 = values[max(0, index - 5) : index + 1]

        ts = datetime.fromisoformat(point["Timestamp"])
        open_minutes = 10 * 60
        now_minutes = ts.hour * 60 + ts.minute

        features.append(
            {
                "Timestamp": point["Timestamp"],
                "RawChartTime": point["RawChartTime"],
                "NormalizedTime24h": point["NormalizedTime24h"],
                "Value": value,
                "PreviousValue": prev,
                "PointChange": point_change,
                "Return5mPercent": return_percent,
                "Direction": direction,
                "SessionOpen": session_open,
                "ChangeFromOpen": value - session_open,
                "ChangeFromOpenPercent": _safe_percent(value - session_open, session_open, neutral=0.0),
                "RunningHigh": running_high,
                "RunningLow": running_low,
                "DistanceFromRunningHigh": running_high - value,
                "DistanceFromRunningLow": value - running_low,
                "PositionInObservedRangePercent": (
                    _safe_percent(value - running_low, observed_range, neutral=50.0)
                    if observed_range != 0
                    else 50.0
                ),
                "MovingAverage3": sum(window3) / len(window3),
                "MovingAverage6": sum(window6) / len(window6),
                "RollingStd3": statistics.pstdev(window3),
                "RollingStd6": statistics.pstdev(window6),
                "MinutesFromOpen": now_minutes - open_minutes,
                "Hour": ts.hour,
                "Minute": ts.minute,
                "IntervalMinutes": INTERVAL_MINUTES,
                "Source": SOURCE_NAME,
                "DataQualityStatus": "OK",
            }
        )

    return features


def build_output_paths(output_root: Path, session_date: date) -> dict[str, Path]:
    base = output_root / f"{session_date:%Y}" / f"{session_date:%m}" / f"{session_date:%d}"
    token = f"{session_date:%Y-%m-%d}"
    return {
        "daily_csv": base / f"egx30_daily_{token}.csv",
        "daily_json": base / f"egx30_daily_{token}.json",
        "points_csv": base / f"egx30_5minute_{token}.csv",
        "points_json": base / f"egx30_5minute_{token}.json",
        "features_csv": base / f"egx30_features_{token}.csv",
        "features_json": base / f"egx30_features_{token}.json",
    }


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]], field_order: list[str]) -> None:
    if not rows:
        raise CollectionError(f"Refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False) as handle:
        writer = csv.DictWriter(handle, fieldnames=field_order)
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def save_outputs(
    output_root: Path,
    session_date: date,
    daily: dict[str, Any],
    points: list[dict[str, Any]],
    features: list[dict[str, Any]],
) -> dict[str, Path]:
    if not points:
        raise CollectionError("Refusing to write outputs without chart points")
    if not features:
        raise CollectionError("Refusing to write outputs without features")

    paths = build_output_paths(output_root, session_date)
    _atomic_write_csv(paths["daily_csv"], [daily], DAILY_FIELD_ORDER)
    _atomic_write_json(paths["daily_json"], daily)
    _atomic_write_csv(paths["points_csv"], points, POINT_FIELD_ORDER)
    _atomic_write_json(paths["points_json"], points)
    _atomic_write_csv(paths["features_csv"], features, FEATURE_FIELD_ORDER)
    _atomic_write_json(paths["features_json"], features)
    return paths


def extract_raw_capture(page: Any) -> RawCapture:
    payload = page.evaluate(
        """
        () => {
          const toText = (el) => (el?.textContent || '').replace(/\u00a0/g, ' ').trim();
          const selected = document.querySelector('select option:checked');
          const pairs = [];

          for (const row of Array.from(document.querySelectorAll('tr'))) {
            const cells = Array.from(row.querySelectorAll('td,th')).map(toText).filter(Boolean);
            if (cells.length >= 2) {
              pairs.push({ label: cells[0], value: cells[1] });
            }
          }

          for (const el of Array.from(document.querySelectorAll('[id*=lbl], [id*=Label], [id*=value]'))) {
            const id = el.id || '';
            const text = toText(el);
            if (id && text) {
              pairs.push({ label: id, value: text });
            }
          }

          const chart = [];
          for (const area of Array.from(document.querySelectorAll('map area'))) {
            const raw = area.getAttribute('title') || area.getAttribute('alt') || '';
            if (raw.trim()) chart.push(raw.trim());
          }

          for (const script of Array.from(document.querySelectorAll('script'))) {
            const text = script.textContent || '';
            const matches = text.match(/\d{1,2}:\d{2}\s*(?:AM|PM|am|pm|ص|م)?[^\n\r,;]*[-+]?\d[\d,]*(?:\.\d+)?/g) || [];
            for (const m of matches) {
              chart.push(m.trim());
            }
          }

          return {
            selected_index: selected ? toText(selected) : '',
            key_value_pairs: pairs,
            chart_raw_points: chart,
          };
        }
        """
    )

    return RawCapture(
        selected_index=payload.get("selected_index", ""),
        key_value_pairs=payload.get("key_value_pairs", []),
        chart_raw_points=payload.get("chart_raw_points", []),
    )


def _clean_key(value: str) -> str:
    value = _normalize_space(_to_ascii_digits(value)).lower()
    value = value.replace("%", "").replace("_", " ")
    value = re.sub(r"[^\w\s]", "", value)
    return value


def build_daily_summary(capture: RawCapture, collected_at: str) -> tuple[dict[str, Any], date]:
    mapping = {
        "Date": ["date", "التاريخ"],
        "Open": ["open", "الافتتاح"],
        "High": ["high", "اعلى", "الاعلى"],
        "Low": ["low", "ادنى", "الادنى"],
        "Close": ["close", "الاغلاق", "الإغلاق"],
        "ChangePercent": ["change", "change percent", "التغير"],
        "YTDPercent": ["ytd", "منذ بداية العام"],
        "IndexValueUSD": ["usd", "index value usd", "قيمة المؤشر بالدولار"],
        "ChangePercentUSD": ["change percent usd", "التغير بالدولار"],
        "TotalTradedValueEGP": ["total traded value", "قيمة التداول"],
        "TotalTradedValueMarketPercent": ["traded value market", "نسبة قيمة التداول"],
        "TotalVolume": ["total volume", "حجم التداول"],
        "TotalVolumeMarketPercent": ["volume market", "نسبة حجم التداول"],
        "TradesCount": ["trades", "عدد العمليات"],
        "TradesMarketPercent": ["trades market", "نسبة العمليات"],
        "MarketCapEGP": ["market cap", "راس المال", "رأس المال"],
        "MarketCapMarketPercent": ["market cap market", "نسبة راس المال", "نسبة رأس المال"],
    }

    collected: dict[str, str] = {}
    for pair in capture.key_value_pairs:
        key = _clean_key(pair.get("label", ""))
        value = _normalize_space(pair.get("value", ""))
        if not key or not value:
            continue
        for field, aliases in mapping.items():
            if any(alias in key for alias in aliases):
                collected.setdefault(field, value)

    if "Date" not in collected:
        raise CollectionError("Missing session date")

    session_date = parse_egx_date(collected["Date"])

    daily: dict[str, Any] = {"Date": f"{session_date:%Y-%m-%d}"}
    for field in REQUIRED_NUMERIC_DAILY_FIELDS:
        if field not in collected:
            raise CollectionError(f"Missing required daily field: {field}")
        daily[field] = parse_number(collected[field])

    daily["Source"] = SOURCE_NAME
    daily["CollectedAt"] = collected_at

    validate_ohlc(daily)

    for field in REQUIRED_NUMERIC_DAILY_FIELDS:
        if not math.isfinite(float(daily[field])):
            raise CollectionError(f"Invalid non-finite value: {field}")

    return daily, session_date


def build_chart_points(raw_points: list[str], session_date: date, cutoff_hhmm: str, collected_at: str) -> list[dict[str, Any]]:
    cutoff_hour, cutoff_minute = parse_hhmm(cutoff_hhmm)
    cutoff_minutes = cutoff_hour * 60 + cutoff_minute

    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_minutes: int | None = None

    for raw in raw_points:
        raw = _normalize_space(raw)
        if not raw:
            continue
        try:
            raw_time, value = parse_chart_entry(raw)
            normalized_time, minutes = normalize_chart_time(raw_time, previous_minutes)
            previous_minutes = minutes

            if minutes > cutoff_minutes:
                continue

            ts = datetime.combine(session_date, datetime.min.time()) + timedelta(minutes=minutes)
            iso_ts = ts.strftime("%Y-%m-%dT%H:%M:00")

            if iso_ts in seen:
                continue
            seen.add(iso_ts)

            points.append(
                {
                    "Timestamp": iso_ts,
                    "RawChartTime": raw_time,
                    "NormalizedTime24h": normalized_time,
                    "Value": value,
                    "IntervalMinutes": INTERVAL_MINUTES,
                    "Source": SOURCE_NAME,
                    "CollectedAt": collected_at,
                }
            )
        except ValueError:
            continue

    points.sort(key=lambda item: item["Timestamp"])
    return points


def validate_collected_data(daily: dict[str, Any], points: list[dict[str, Any]], selected_index: str) -> None:
    if "egx30" not in selected_index.lower().replace(" ", ""):
        raise CollectionError(f"Selected index is not EGX30: {selected_index}")

    if not points:
        raise CollectionError("No chart points were extracted")

    for field in REQUIRED_NUMERIC_DAILY_FIELDS:
        value = float(daily[field])
        if not math.isfinite(value):
            raise CollectionError(f"Invalid numeric daily value: {field}")

    for point in points:
        value = float(point["Value"])
        if not math.isfinite(value):
            raise CollectionError("Invalid numeric chart point value")


def save_diagnostics(page: Any | None, attempt_dir: Path, message: str) -> None:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "error.txt").write_text(message + "\n", encoding="utf-8")

    if page is None:
        return

    try:
        page.screenshot(path=str(attempt_dir / "page.png"), full_page=True)
    except Exception as screenshot_error:
        LOG.warning("Failed to save screenshot: %s", screenshot_error)

    try:
        html = page.content()
        (attempt_dir / "page.html").write_text(html, encoding="utf-8")
    except Exception as html_error:
        LOG.warning("Failed to save html diagnostic: %s", html_error)


def _ensure_egx30_selection(page: Any) -> str:
    selection = page.evaluate(
        """
        () => {
          const select = document.querySelector('select[id*=Indices], select[name*=Indices], select');
          if (!select) {
            return '';
          }
          const options = Array.from(select.options || []);
          const selected = options.find(o => o.selected);
          return selected ? selected.textContent.trim() : '';
        }
        """
    )

    if "EGX30" in selection.upper().replace(" ", ""):
        return selection

    page.evaluate(
        """
        () => {
          const select = document.querySelector('select[id*=Indices], select[name*=Indices], select');
          if (!select) return;
          const options = Array.from(select.options || []);
          const target = options.find(o => (o.textContent || '').toUpperCase().replace(/\s+/g, '').includes('EGX30'));
          if (!target) return;
          select.value = target.value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """
    )

    page.wait_for_timeout(500)
    return page.evaluate(
        """
        () => {
          const select = document.querySelector('select[id*=Indices], select[name*=Indices], select');
          if (!select) return '';
          const selected = Array.from(select.options || []).find(o => o.selected);
          return selected ? selected.textContent.trim() : '';
        }
        """
    )


def collect_once(headed: bool, attempt_dir: Path) -> RawCapture:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(locale="ar-EG", timezone_id=TIMEZONE_NAME)
        page = context.new_page()
        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_load_state("networkidle", timeout=60_000)

            selected_index = _ensure_egx30_selection(page)

            selectors = [
                "map area",
                "img[usemap]",
                "table tr",
            ]
            for selector in selectors:
                try:
                    page.wait_for_selector(selector, state="attached", timeout=30_000)
                except PlaywrightTimeoutError:
                    continue

            capture = extract_raw_capture(page)
            if not capture.selected_index:
                capture.selected_index = selected_index

            return capture
        except Exception as error:
            message = f"Collection browser attempt failed: {type(error).__name__}: {error}"
            save_diagnostics(page, attempt_dir, message)
            raise
        finally:
            browser.close()


def run_collection(args: argparse.Namespace) -> dict[str, Path]:
    output_root = Path(args.output_root)
    debug_root = Path(args.debug_dir)
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    last_error: Exception | None = None

    for attempt in range(1, args.max_attempts + 1):
        LOG.info("Collection attempt %s/%s", attempt, args.max_attempts)
        attempt_dir = debug_root / run_id / f"attempt_{attempt}"
        try:
            capture = collect_once(args.headed, attempt_dir)
            collected_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

            daily, session_date = build_daily_summary(capture, collected_at)
            points = build_chart_points(capture.chart_raw_points, session_date, args.market_close, collected_at)
            validate_collected_data(daily, points, capture.selected_index)
            features = build_features(points)

            paths = save_outputs(output_root, session_date, daily, points, features)
            LOG.info("Collection completed for session date %s", daily["Date"])
            return paths

        except Exception as error:  # pragma: no cover - exercised in runtime
            last_error = error
            message = f"Attempt {attempt} failed: {type(error).__name__}: {error}"
            LOG.error(message)
            (attempt_dir / "error.txt").write_text(message + "\n", encoding="utf-8")

            if attempt < args.max_attempts:
                time.sleep(2 * attempt)

    raise CollectionError(f"Collection failed after {args.max_attempts} attempts: {last_error}")


def main() -> int:
    configure_logging()
    args = parse_args()

    try:
        paths = run_collection(args)
    except Exception as error:
        LOG.error("EGX30 collection failed: %s", error)
        return 1

    for key, path in paths.items():
        LOG.info("%s -> %s", key, path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
