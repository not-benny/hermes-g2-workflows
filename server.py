"""Dependency-free, policy-owning Hermes G2 workflow MCP server.

The plugin launcher deliberately uses the system Python. Keeping this stdio
MCP surface dependency-free makes the package portable while preserving the
wire contract used by Hermes' installed MCP SDK.

Model arguments contain only user intent. Hermes attaches one signed,
package-bound capability in MCP request ``_meta``. This untrusted standalone
process can only pass that opaque authority unchanged to the in-process native
relay, where its signature, live turn and bounded use are verified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ``python -I -S`` deliberately removes the script directory as well as user
# and site-package paths. Re-add only this digest-checked package root so its
# two local modules can load without reopening ambient import authority.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from device_workflows import (
    DEVICE_TOOLS,
    DEVICE_TOOL_NAMES,
    DeviceInputError,
    DeviceOutputError,
    normalize_device_invocation,
    validate_device_result,
)
from relay_client import MAX_BYTES, RelayError, call_relay


SERVER_VERSION = "0.3.0"
SERVER_NAME = "hermes-g2-workflows"
CAPABILITY_META_KEY = "com.hermes/capability"
CAPABILITY_AUDIENCE = "com.hermes.mcp/portable/hermes-g2-workflows/workflows"
CAPABILITY_BINDING = "hermes-g2-workflows:workflows"
LATEST_HANDSHAKE_VERSION = "2025-11-25"
HANDSHAKE_VERSIONS = frozenset({
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
})
LONDON = ZoneInfo("Europe/London")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_RESULT_TEXT_BYTES = 24 * 1024
MAX_SESSION_FIELD_BYTES = 512
MAX_TOOL_TEXT_BYTES = 480
MAX_NOTIFY_CHARS = 160
MAX_CLOCK_DURATION_SECONDS = 604_800

_CRS = re.compile(r"^[A-Z0-9]{3}$")
_CLOCK_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_DASHBOARD_KEY = re.compile(r"^weather-[a-f0-9]{32}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONTEXT_DASHBOARD_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_WORK_TASK_ID = re.compile(r"^wt_[a-f0-9]{32}$")
_CLOCK_ITEM_ID = re.compile(r"^clk_[a-f0-9]{32}$")
_REMINDER_ID = re.compile(r"^[a-f0-9]{32}$")
_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_SET = frozenset(_WEEKDAYS)
_CAPABILITY_CLAIM_FIELDS = frozenset({
    "version",
    "audience",
    "binding",
    "package_digest",
    "platform",
    "chat_id",
    "session_id",
    "message_id",
    "profile",
    "tool_call_id",
    "workflow",
    "arguments_sha256",
    "issued_at",
    "expires_at",
    "nonce",
})
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_PACKAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BIDI_CONTROLS = frozenset({
    0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
})
_CONDITIONS = {
    "clear": "Clear",
    "partly_cloudy": "Partly cloudy",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "drizzle": "Drizzle",
    "rain": "Rain",
    "snow": "Snow",
    "showers": "Showers",
    "snow_showers": "Snow showers",
    "thunderstorm": "Thunderstorm",
}
_CONDITION_BY_CODE = {
    0: "clear", 1: "partly_cloudy", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "drizzle", 57: "drizzle", 61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain", 71: "snow", 73: "snow", 75: "snow",
    77: "snow", 80: "showers", 81: "showers", 82: "showers",
    85: "snow_showers", 86: "snow_showers", 95: "thunderstorm",
    96: "thunderstorm", 99: "thunderstorm",
}
_TRAIN_KINDS = frozenset({"live", "no_live_services", "next_scheduled", "no_services"})
_TRAIN_STATUSES = frozenset({"on_time", "delayed", "cancelled", "unknown"})

RelayCall = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]


class ToolInputError(ValueError):
    """Safe model-input validation failure."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode(value: str) -> dict[str, Any]:
    parsed = json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_nonfinite,
    )
    if not isinstance(parsed, dict):
        raise ValueError("workflow result is not an object")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _stable_operation(prefix: str, claims: Mapping[str, Any]) -> str:
    """Derive a retry-stable, content-free ID from trusted call metadata."""
    canonical = _json({
        "version": 1,
        "prefix": prefix,
        "audience": claims["audience"],
        "package_digest": claims["package_digest"],
        "profile": claims["profile"],
        "session_id": claims["session_id"],
        "message_id": claims["message_id"],
        "tool_call_id": claims["tool_call_id"],
    })
    value = f"{prefix}.{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"
    if not _OPERATION_ID.fullmatch(value):
        raise RuntimeError("operation id generation failed")
    return value


def _trusted_capability(
    meta: Any, workflow: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Extract an exact opaque capability without treating its claims as trusted."""
    if not isinstance(meta, dict) or set(meta) != {CAPABILITY_META_KEY}:
        raise PermissionError("A trusted Hermes workflow capability is required")
    value = meta.get(CAPABILITY_META_KEY)
    if not isinstance(value, dict) or set(value) != {"claims", "signature"}:
        raise PermissionError("Hermes workflow capability is malformed")
    claims = value.get("claims")
    signature = value.get("signature")
    if not isinstance(claims, dict) or set(claims) != _CAPABILITY_CLAIM_FIELDS:
        raise PermissionError("Hermes workflow capability is malformed")
    if not isinstance(signature, str) or _HEX_64.fullmatch(signature) is None:
        raise PermissionError("Hermes workflow capability is malformed")
    if type(claims.get("version")) is not int or claims["version"] != 1:
        raise PermissionError("Hermes workflow capability is malformed")
    for key in (
        "audience", "binding", "platform", "profile", "chat_id",
        "session_id", "message_id", "tool_call_id", "workflow",
    ):
        if not _safe_identity(claims.get(key)):
            raise PermissionError("Hermes workflow capability is malformed")
    if (
        claims.get("audience") != CAPABILITY_AUDIENCE
        or claims.get("binding") != CAPABILITY_BINDING
        or claims.get("workflow") != workflow
        or claims.get("platform") != "g2"
        or claims.get("profile") != "even-g2"
        or not str(claims.get("message_id", "")).startswith("g2-turn-")
        or _PACKAGE_DIGEST.fullmatch(str(claims.get("package_digest", ""))) is None
        or _HEX_64.fullmatch(str(claims.get("arguments_sha256", ""))) is None
        or _NONCE.fullmatch(str(claims.get("nonce", ""))) is None
        or type(claims.get("issued_at")) is not int
        or type(claims.get("expires_at")) is not int
        or not 0 < claims["issued_at"] < claims["expires_at"] <= MAX_SAFE_INTEGER
    ):
        raise PermissionError("Hermes workflow capability is malformed")
    argument_digest = hashlib.sha256(_json(dict(arguments)).encode("utf-8")).hexdigest()
    if claims["arguments_sha256"] != argument_digest:
        raise PermissionError("Hermes workflow capability arguments do not match")
    # A JSON round-trip both detaches the envelope and rejects non-finite data.
    return json.loads(_json(value), parse_constant=_reject_nonfinite)


def _safe_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_SESSION_FIELD_BYTES
        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value)
    )


def _safe_line(value: Any, *, max_chars: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ToolInputError("Expected bounded text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > max_chars
        or len(normalized.encode("utf-8")) > max_bytes
        or any(char in "\r\n\x00" or ord(char) in _BIDI_CONTROLS for char in normalized)
        or any(unicodedata.category(char) == "Cc" for char in normalized)
    ):
        raise ToolInputError("Expected one bounded inert line")
    return normalized


def _exact_args(arguments: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolInputError("Tool arguments must be an object")
    optional = optional or set()
    keys = set(arguments)
    if not required <= keys or keys - required - optional:
        raise ToolInputError("Tool arguments do not match the intent schema")
    return dict(arguments)


def _finite_number(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("typed number is missing")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("typed number is outside bounds")
    return number


def _bounded_epoch(value: Any) -> int:
    if type(value) is not int or not 0 < value <= MAX_SAFE_INTEGER:
        raise ValueError("typed timestamp is invalid")
    return value


def _typed_weather_read(read: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if read.get("success") is not True:
        if set(read) - {"success", "state", "error_code", "error"} or read.get("success") is not False:
            raise ValueError("typed weather failure is malformed")
        state = read.get("state")
        code = read.get("error_code")
        error = read.get("error")
        if state not in {"error", "offline"} or not isinstance(code, str) or not _safe_identity(error):
            raise ValueError("typed weather failure is malformed")
        return None, code

    if set(read) != {"success", "trust", "dashboard_key", "title", "result"}:
        raise ValueError("typed weather envelope is malformed")
    if read.get("trust") != "typed_open_meteo_ukmo_data":
        raise ValueError("typed weather provenance is missing")
    key = read.get("dashboard_key")
    title = read.get("title")
    result = read.get("result")
    if (
        not isinstance(key, str)
        or not _DASHBOARD_KEY.fullmatch(key)
        or not isinstance(title, str)
        or not isinstance(result, dict)
    ):
        raise ValueError("typed weather identity is malformed")
    expected = {
        "location_label", "date", "weather_code", "condition",
        "temperature_min_c", "temperature_max_c",
        "precipitation_probability_max_pct", "precipitation_amount_mm",
        "wind_speed_max_kmh", "source", "observed_at_ms",
    }
    if set(result) != expected:
        raise ValueError("typed weather result is malformed")
    location = _safe_line(result.get("location_label"), max_chars=120, max_bytes=240)
    forecast_date = result.get("date")
    if not isinstance(forecast_date, str):
        raise ValueError("typed weather date is malformed")
    try:
        Date.fromisoformat(forecast_date)
    except ValueError as exc:
        raise ValueError("typed weather date is malformed") from exc
    title = _safe_line(title, max_chars=48, max_bytes=192)
    weather_code = result.get("weather_code")
    condition = result.get("condition")
    if type(weather_code) is not int or _CONDITION_BY_CODE.get(weather_code) != condition:
        raise ValueError("typed weather condition is malformed")
    low = _finite_number(result.get("temperature_min_c"), -90, 65)
    high = _finite_number(result.get("temperature_max_c"), -90, 65)
    if low > high:
        raise ValueError("typed weather temperature range is malformed")
    probability = result.get("precipitation_probability_max_pct")
    if probability is not None and (type(probability) is not int or not 0 <= probability <= 100):
        raise ValueError("typed weather probability is malformed")
    amount = _finite_number(result.get("precipitation_amount_mm"), 0, 2_000)
    wind = _finite_number(result.get("wind_speed_max_kmh"), 0, 500)
    if result.get("source") != "Open-Meteo · UK Met Office data":
        raise ValueError("typed weather source is malformed")
    observed = _bounded_epoch(result.get("observed_at_ms"))
    return {
        "dashboard_key": key,
        "title": title,
        "location_label": location,
        "date": forecast_date,
        "weather_code": weather_code,
        "condition": condition,
        "temperature_min_c": low,
        "temperature_max_c": high,
        "precipitation_probability_max_pct": probability,
        "precipitation_amount_mm": amount,
        "wind_speed_max_kmh": wind,
        "observed_at_ms": observed,
    }, None


def _weather_spec(read: dict[str, Any]) -> tuple[dict[str, Any], str, str, str, bool]:
    result, error_code = _typed_weather_read(read)
    if result is None:
        ambiguous = error_code == "ambiguous_location"
        primary = "Location needs a county or region" if ambiguous else "Weather is unavailable"
        offline = error_code == "unavailable"
        spec = {
            "version": 2,
            "presentation_mode": "deck",
            "dashboard_key": "weather-error",
            "title": "Weather forecast",
            "state": "offline" if offline else "error",
            "privacy": "private",
            "summary": {"primary": primary, "uncertainty": "unknown"},
            "sections": [{
                "id": "forecast-error", "order": 0, "type": "message",
                "title": "Forecast", "load_state": "error",
                "source_ids": ["open-meteo-ukmo"], "uncertainty": "unknown",
                "error_code": "offline" if offline else "invalid_data", "body": primary,
            }],
            "sources": [{
                "id": "open-meteo-ukmo",
                "label": "Open-Meteo · UK Met Office data",
                "attribution_id": "open_meteo_ukmo",
                "stale_after_seconds": 900,
                "status": "unavailable",
            }],
            "local_actions": [],
            "ttl_seconds": 120,
        }
        return spec, "weather-error", "Weather forecast", primary, False

    condition = _CONDITIONS[result["condition"]]
    low = result["temperature_min_c"]
    high = result["temperature_max_c"]
    amount = result["precipitation_amount_mm"]
    probability = result["precipitation_probability_max_pct"]
    rain = f"{probability}% · {amount:g} mm" if probability is not None else f"{amount:g} mm"
    primary = f"{condition} · {low:g}–{high:g}°C"
    spec = {
        "version": 2,
        "presentation_mode": "deck",
        "dashboard_key": result["dashboard_key"],
        "title": result["title"],
        "state": "ready",
        "privacy": "private",
        "summary": {
            "primary": primary,
            "secondary": f"{result['location_label']} · {result['date']}",
            "uncertainty": "estimated",
        },
        "sections": [{
            "id": "forecast", "order": 0, "type": "status_grid",
            "title": "Daily outlook", "load_state": "ready",
            "source_ids": ["open-meteo-ukmo"], "uncertainty": "estimated",
            "rows": [
                {"id": "condition", "label": "Condition", "value": condition},
                {"id": "temperature", "label": "Low / high", "value": f"{low:g}°C / {high:g}°C"},
                {"id": "precipitation", "label": "Rain", "value": rain},
                {"id": "wind", "label": "Max wind", "value": f"{result['wind_speed_max_kmh']:g} km/h"},
            ],
        }],
        "sources": [{
            "id": "open-meteo-ukmo",
            "label": "Open-Meteo · UK Met Office data",
            "attribution_id": "open_meteo_ukmo",
            "observed_at_ms": result["observed_at_ms"],
            "stale_after_seconds": 900,
            "status": "current",
        }],
        "local_actions": [],
        "ttl_seconds": 900,
    }
    return spec, result["dashboard_key"], result["title"], primary, True


def _typed_train_read(read: dict[str, Any], origin: str, destination: str) -> dict[str, Any] | None:
    if read.get("success") is not True:
        if set(read) != {"success", "error"} or read.get("success") is not False or not _safe_identity(read.get("error")):
            raise ValueError("typed train failure is malformed")
        return None
    if set(read) != {"success", "trust", "result"} or read.get("trust") != "typed_national_rail_data":
        raise ValueError("typed train envelope is malformed")
    result = read.get("result")
    required = {"source", "origin_crs", "destination_crs", "data_kind", "observed_at_ms", "departures"}
    if not isinstance(result, dict) or set(result) != required:
        raise ValueError("typed train result is malformed")
    if (
        result.get("source") != "National Rail"
        or result.get("origin_crs") != origin
        or result.get("destination_crs") != destination
        or result.get("data_kind") not in _TRAIN_KINDS
    ):
        raise ValueError("typed train route is malformed")
    observed = _bounded_epoch(result.get("observed_at_ms"))
    raw_rows = result.get("departures")
    if not isinstance(raw_rows, list) or len(raw_rows) > 12:
        raise ValueError("typed train rows are malformed")
    rows: list[dict[str, Any]] = []
    previous = -1
    for raw in raw_rows:
        required_row = {"scheduled_departure_ms", "scheduled_arrival_ms", "status"}
        optional_row = {"expected_departure_ms", "expected_arrival_ms", "platform", "changes"}
        if not isinstance(raw, dict) or not required_row <= set(raw) or set(raw) - required_row - optional_row:
            raise ValueError("typed train row is malformed")
        scheduled = _bounded_epoch(raw.get("scheduled_departure_ms"))
        arrival = _bounded_epoch(raw.get("scheduled_arrival_ms"))
        if scheduled < previous or arrival < scheduled or raw.get("status") not in _TRAIN_STATUSES:
            raise ValueError("typed train row is malformed")
        previous = scheduled
        row: dict[str, Any] = {
            "scheduled_departure_ms": scheduled,
            "scheduled_arrival_ms": arrival,
            "status": raw["status"],
        }
        for key in ("expected_departure_ms", "expected_arrival_ms"):
            if key in raw:
                row[key] = _bounded_epoch(raw[key])
        if "platform" in raw:
            row["platform"] = _safe_line(raw["platform"], max_chars=16, max_bytes=32)
        if "changes" in raw:
            changes = raw["changes"]
            if type(changes) is not int or not 0 <= changes <= 7:
                raise ValueError("typed train changes are malformed")
            row["changes"] = changes
        rows.append(row)
    return {"observed_at_ms": observed, "departures": rows}


def _time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=LONDON).strftime("%H:%M")


def _train_spec(read: dict[str, Any], origin: str, destination: str) -> tuple[dict[str, Any], str, str, str]:
    result = _typed_train_read(read, origin, destination)
    digest = hashlib.sha256(f"{origin}:{destination}".encode("ascii")).hexdigest()[:24]
    key = f"rail-{digest}"
    title = f"{origin} to {destination}"
    departures = result["departures"] if result is not None else []
    if departures:
        rows = []
        for index, item in enumerate(departures[:6]):
            row = {
                "id": f"service-{index + 1}",
                "destination": destination,
                "scheduled_departure_ms": item["scheduled_departure_ms"],
                "status": item["status"],
            }
            for field in ("expected_departure_ms", "platform"):
                if field in item:
                    row[field] = item[field]
            rows.append(row)
        first = rows[0]
        shown = int(first.get("expected_departure_ms") or first["scheduled_departure_ms"])
        primary = f"Next {destination} · {_time(shown)}"
        state, load_state, source_status, uncertainty = "ready", "ready", "current", "exact"
        error_code = None
    else:
        primary = "No train services found" if result is not None else "Train times unavailable"
        state = "empty" if result is not None else "offline"
        load_state = "empty" if result is not None else "error"
        source_status = "current" if result is not None else "unavailable"
        uncertainty = "unknown"
        error_code = "offline" if result is None else None
        rows = []
    section: dict[str, Any] = {
        "id": "departures", "order": 0, "type": "departures", "title": "Departures",
        "load_state": load_state, "source_ids": ["national-rail"],
        "uncertainty": uncertainty, "rows": rows,
    }
    if error_code:
        section["error_code"] = error_code
    source: dict[str, Any] = {
        "id": "national-rail", "label": "National Rail",
        "stale_after_seconds": 120, "status": source_status,
    }
    if result is not None:
        source["observed_at_ms"] = result["observed_at_ms"]
    spec = {
        "version": 2, "presentation_mode": "deck", "dashboard_key": key,
        "title": title, "state": state, "privacy": "private",
        "summary": {"primary": primary, "uncertainty": uncertainty},
        "sections": [section], "sources": [source], "local_actions": [],
        "ttl_seconds": 120,
    }
    return spec, key, title, primary


def _decode_present_result(
    value: Any,
    *,
    operation_id: str,
    dashboard_key: str,
) -> dict[str, Any]:
    """Require proof that this exact atomic deck reached an acknowledged frame."""
    if (
        not isinstance(operation_id, str)
        or _OPERATION_ID.fullmatch(operation_id) is None
        or not isinstance(dashboard_key, str)
        or _OPERATION_ID.fullmatch(dashboard_key) is None
        or not isinstance(value, dict)
        or set(value) != {"success", "receipt"}
        or value.get("success") is not True
        or not isinstance(value.get("receipt"), dict)
    ):
        raise ValueError("context presentation receipt is malformed")
    receipt = value["receipt"]
    if (
        set(receipt)
        != {
            "status",
            "operation_id",
            "dashboard_key",
            "dashboard_id",
            "presentation_generation",
            "refresh_generation",
            "revision",
            "frame_id",
        }
        or receipt.get("status")
        not in {"acknowledged", "historical_acknowledgement"}
        or receipt.get("operation_id") != operation_id
        or receipt.get("dashboard_key") != dashboard_key
        or not isinstance(receipt.get("dashboard_id"), str)
        or _CONTEXT_DASHBOARD_ID.fullmatch(receipt["dashboard_id"]) is None
        or type(receipt.get("presentation_generation")) is not int
        or receipt["presentation_generation"] != 1
        or type(receipt.get("refresh_generation")) is not int
        or receipt["refresh_generation"] != 1
        or type(receipt.get("revision")) is not int
        or receipt["revision"] != 1
        or type(receipt.get("frame_id")) is not int
        or not 1 <= receipt["frame_id"] <= MAX_SAFE_INTEGER
    ):
        raise ValueError("context presentation receipt is malformed")
    return receipt


def _public_failure(value: Any, fallback: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("success") is not False:
        return {"success": False, "state": "error", "error": fallback}
    if set(value) not in (
        {"success", "commit_state", "error"},
        {"success", "commit_state", "operation_id", "error"},
    ):
        return {"success": False, "state": "error", "error": fallback}
    commit_state = value.get("commit_state")
    error = value.get("error")
    if commit_state not in {"not_committed", "unknown"} or not _safe_identity(error):
        return {"success": False, "state": "error", "error": fallback}
    return {
        "success": False,
        "state": "outcome_unknown" if commit_state == "unknown" else "not_committed",
        "error": _safe_line(error, max_chars=240, max_bytes=960),
    }


def _mutation_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"success", "receipt"}:
        raise ValueError("mutation receipt envelope is malformed")
    if value.get("success") is not True or not isinstance(value.get("receipt"), dict):
        raise ValueError("mutation receipt envelope is malformed")
    return value["receipt"]


def _decode_work_task_result(
    value: Any, *, operation_id: str, lane: str
) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("success") is False:
        return _public_failure(value, "Work Tasks returned an invalid receipt")
    receipt = _mutation_envelope(value)
    if set(receipt) != {"status", "operation_id", "task_id", "lane", "board_revision"}:
        raise ValueError("work task receipt is malformed")
    if (
        receipt.get("status") not in {"acknowledged", "historical_acknowledgement"}
        or receipt.get("operation_id") != operation_id
        or not isinstance(receipt.get("task_id"), str)
        or _WORK_TASK_ID.fullmatch(receipt["task_id"]) is None
        or receipt.get("lane") != lane
        or type(receipt.get("board_revision")) is not int
        or not 1 <= receipt["board_revision"] <= MAX_SAFE_INTEGER
    ):
        raise ValueError("work task receipt is malformed")
    return {
        "success": True,
        "state": "completed",
        "status": receipt["status"],
        "task_id": receipt["task_id"],
        "lane": lane,
        "board_revision": receipt["board_revision"],
    }


def _decode_clock_result(
    value: Any,
    *,
    operation_id: str,
    kind: str,
    duration_seconds: int | None = None,
    local_time: str | None = None,
    expected_date: str | None = None,
    expected_repeat_days: list[str] | None = None,
    allow_resolved_date: bool = False,
) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("success") is False:
        return _public_failure(value, "Clock returned an invalid receipt")
    receipt = _mutation_envelope(value)
    common = {
        "status", "operation_id", "item_id", "kind", "next_fire_at_ms",
        "clock_revision",
    }
    specific = {"duration_seconds"} if kind == "timer" else {"local_time", "date", "repeat_days"}
    if set(receipt) != common | specific:
        raise ValueError("clock receipt is malformed")
    if (
        receipt.get("status") not in {"acknowledged", "historical_acknowledgement"}
        or receipt.get("operation_id") != operation_id
        or not isinstance(receipt.get("item_id"), str)
        or _CLOCK_ITEM_ID.fullmatch(receipt["item_id"]) is None
        or receipt.get("kind") != kind
        or type(receipt.get("next_fire_at_ms")) is not int
        or not 1 <= receipt["next_fire_at_ms"] <= MAX_SAFE_INTEGER
        or type(receipt.get("clock_revision")) is not int
        or not 1 <= receipt["clock_revision"] <= MAX_SAFE_INTEGER
    ):
        raise ValueError("clock receipt is malformed")
    result = {
        "success": True,
        "state": "completed",
        "status": receipt["status"],
        "item_id": receipt["item_id"],
        "kind": kind,
        "next_fire_at_ms": receipt["next_fire_at_ms"],
        "clock_revision": receipt["clock_revision"],
    }
    if kind == "timer":
        if type(receipt.get("duration_seconds")) is not int or receipt["duration_seconds"] != duration_seconds:
            raise ValueError("clock receipt is malformed")
        result["duration_seconds"] = receipt["duration_seconds"]
    else:
        receipt_date = receipt.get("date")
        receipt_days = receipt.get("repeat_days")
        if (
            receipt.get("local_time") != local_time
            or not isinstance(receipt_date, (str, type(None)))
            or not isinstance(receipt_days, list)
            or any(not isinstance(day, str) or day not in _WEEKDAY_SET for day in receipt_days)
            or len(set(receipt_days)) != len(receipt_days)
        ):
            raise ValueError("clock receipt is malformed")
        expected_days = [] if expected_repeat_days is None else expected_repeat_days
        if allow_resolved_date:
            if expected_date is not None or expected_days or not isinstance(receipt_date, str):
                raise ValueError("clock receipt is malformed")
            try:
                if Date.fromisoformat(receipt_date).isoformat() != receipt_date:
                    raise ValueError("clock receipt is malformed")
            except ValueError as exc:
                raise ValueError("clock receipt is malformed") from exc
        elif receipt_date != expected_date:
            raise ValueError("clock receipt is malformed")
        if receipt_days != expected_days:
            raise ValueError("clock receipt is malformed")
        result.update({
            "local_time": receipt["local_time"],
            "date": receipt_date,
            "repeat_days": list(receipt_days),
        })
    return result


def _decode_reminder_result(value: Any, *, operation_id: str) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("success") is False:
        return _public_failure(value, "Reminder returned an invalid receipt")
    if not isinstance(value, dict) or value.get("success") is not True:
        raise ValueError("reminder receipt is malformed")
    status = value.get("status")
    if status in {"scheduled", "historical_scheduled"}:
        if set(value) != {"success", "status", "operation_id", "reminder_id", "due_at"}:
            raise ValueError("reminder receipt is malformed")
        if (
            value.get("operation_id") != operation_id
            or not isinstance(value.get("reminder_id"), str)
            or _REMINDER_ID.fullmatch(value["reminder_id"]) is None
            or not isinstance(value.get("due_at"), str)
            or _INSTANT.fullmatch(value["due_at"]) is None
        ):
            raise ValueError("reminder receipt is malformed")
        try:
            parsed_due = datetime.fromisoformat(value["due_at"][:-1] + "+00:00")
            canonical_due = parsed_due.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
        except (OverflowError, ValueError) as exc:
            raise ValueError("reminder receipt is malformed") from exc
        if canonical_due != value["due_at"]:
            raise ValueError("reminder receipt is malformed")
        return {
            "success": True,
            "state": "scheduled",
            "status": status,
            "reminder_id": value["reminder_id"],
            "due_at": value["due_at"],
        }
    if status == "historical_delivered":
        if set(value) != {"success", "status", "operation_id", "receipt"}:
            raise ValueError("reminder receipt is malformed")
        receipt = value.get("receipt")
        if (
            value.get("operation_id") != operation_id
            or not isinstance(receipt, dict)
            or set(receipt) != {"status", "operation_id"}
            or receipt.get("operation_id") != operation_id
            or receipt.get("status") not in {"queued", "acknowledged", "historical_acknowledgement"}
        ):
            raise ValueError("reminder receipt is malformed")
        return {
            "success": True,
            "state": "delivered",
            "status": status,
            "delivery_status": receipt["status"],
        }
    raise ValueError("reminder receipt is malformed")


class WorkflowService:
    """Intent-complete tool implementation over the fixed native relay."""

    def __init__(self, relay: RelayCall = call_relay) -> None:
        self._relay_call = relay

    async def call_tool(self, name: str, arguments: Any, meta: Any) -> dict[str, Any]:
        handlers = {
            "g2_work_task_add": self._work_task_add,
            "g2_clock_set_timer": self._clock_set_timer,
            "g2_clock_set_alarm": self._clock_set_alarm,
            "g2_reminder_create": self._reminder_create,
            "g2_weather_present": self._weather_present,
            "g2_train_departures_present": self._train_departures_present,
        }
        handler = handlers.get(name)
        if handler is None and name not in DEVICE_TOOL_NAMES:
            raise ToolInputError("Unknown workflow tool")
        if not isinstance(arguments, dict):
            raise ToolInputError("Tool arguments must be an object")
        # Preserve the exact model-authored JSON object whose digest the host
        # signed. Defaults and normalized native arguments are kept separate.
        try:
            outer_arguments = json.loads(
                _json(arguments),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ToolInputError("Tool arguments must be strict JSON") from exc
        capability = _trusted_capability(meta, name, outer_arguments)
        context = {
            "capability": capability,
            "claims": capability["claims"],
            "workflow": name,
            "workflow_arguments": outer_arguments,
            "next_subcall": 0,
        }
        if name in DEVICE_TOOL_NAMES:
            return await self._device_workflow(name, outer_arguments, context)
        return await handler(outer_arguments, context)

    async def _device_workflow(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            invocation = normalize_device_invocation(name, arguments)
        except DeviceInputError as exc:
            raise ToolInputError(str(exc)) from exc
        try:
            result = await self._relay(
                context, invocation.workflow, invocation.arguments
            )
        except (RelayError, ValueError):
            return {
                "success": False,
                "state": "error",
                "error_code": (
                    "outcome_unknown" if invocation.mutating else "unavailable"
                ),
                "error": (
                    "The phone action may have completed; verify its current state"
                    if invocation.mutating
                    else "The G2 device workflow is unavailable"
                ),
            }
        try:
            return validate_device_result(name, result)
        except DeviceOutputError:
            return {
                "success": False,
                "state": "error",
                "error_code": "phone_error",
                "error": "The phone returned an invalid device workflow result",
            }

    @staticmethod
    def _next_subcall(context: dict[str, Any]) -> int:
        value = context["next_subcall"] + 1
        if type(value) is not int or not 1 <= value <= 8:
            raise RelayError("G2 workflow exceeded its bounded relay sequence")
        context["next_subcall"] = value
        return value

    async def _relay(
        self,
        context: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
        *,
        subcall_id: int | None = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        if subcall_id is None:
            subcall_id = self._next_subcall(context)
        authorization = {
            "capability": context["capability"],
            "workflow": context["workflow"],
            "workflow_arguments": context["workflow_arguments"],
            "subcall_id": subcall_id,
            "attempt": attempt,
        }
        return _decode(await self._relay_call(name, arguments, authorization))

    async def _idempotent(
        self,
        session: dict[str, Any],
        name: str,
        prefix: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "operation_id": _stable_operation(prefix, session["claims"]),
            **arguments,
        }
        subcall_id = self._next_subcall(session)
        try:
            first = await self._relay(
                session, name, payload, subcall_id=subcall_id, attempt=1
            )
        except (RelayError, ValueError):
            return await self._relay(
                session, name, payload, subcall_id=subcall_id, attempt=2
            )
        if first.get("success") is False and first.get("commit_state") == "unknown":
            return await self._relay(
                session, name, payload, subcall_id=subcall_id, attempt=2
            )
        return first

    async def _work_task_add(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"title"}, {"lane"})
        title = _safe_line(args["title"], max_chars=120, max_bytes=MAX_TOOL_TEXT_BYTES)
        lane = args.get("lane", "inbox")
        if lane not in {"inbox", "today", "doing"}:
            raise ToolInputError("lane must be inbox, today, or doing")
        operation_id = _stable_operation("task", session["claims"])
        try:
            value = await self._idempotent(session, "g2.work_tasks.add", "task", {"title": title, "lane": lane})
            return _decode_work_task_result(value, operation_id=operation_id, lane=lane)
        except (RelayError, ValueError):
            return {"success": False, "state": "outcome_unknown", "error": "Work Tasks outcome could not be confirmed"}

    async def _clock_set_timer(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"duration_seconds"}, {"label"})
        duration = args["duration_seconds"]
        if type(duration) is not int or not 1 <= duration <= MAX_CLOCK_DURATION_SECONDS:
            raise ToolInputError("duration_seconds must be an integer from 1 to 604800")
        payload: dict[str, Any] = {"duration_seconds": duration}
        if "label" in args:
            payload["label"] = _safe_line(args["label"], max_chars=80, max_bytes=320)
        operation_id = _stable_operation("timer", session["claims"])
        try:
            value = await self._idempotent(session, "g2.clock.set_timer", "timer", payload)
            return _decode_clock_result(
                value,
                operation_id=operation_id,
                kind="timer",
                duration_seconds=duration,
            )
        except (RelayError, ValueError):
            return {"success": False, "state": "outcome_unknown", "error": "Clock outcome could not be confirmed"}

    async def _clock_set_alarm(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"local_time"}, {"date", "repeat_days", "label"})
        local_time = args["local_time"]
        if not isinstance(local_time, str) or not _CLOCK_TIME.fullmatch(local_time):
            raise ToolInputError("local_time must use exact 24-hour HH:MM format")
        if "date" in args and "repeat_days" in args:
            raise ToolInputError("date and repeat_days are mutually exclusive")
        payload: dict[str, Any] = {"local_time": local_time}
        if "date" in args:
            value = args["date"]
            if not isinstance(value, str):
                raise ToolInputError("date must use YYYY-MM-DD")
            try:
                payload["date"] = Date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise ToolInputError("date must be a real YYYY-MM-DD date") from exc
        if "repeat_days" in args:
            days = args["repeat_days"]
            if (
                not isinstance(days, list)
                or not 1 <= len(days) <= 7
                or any(not isinstance(day, str) or day not in _WEEKDAY_SET for day in days)
                or len(set(days)) != len(days)
            ):
                raise ToolInputError("repeat_days must contain unique lowercase weekdays")
            payload["repeat_days"] = [day for day in _WEEKDAYS if day in days]
        if "label" in args:
            payload["label"] = _safe_line(args["label"], max_chars=80, max_bytes=320)
        operation_id = _stable_operation("alarm", session["claims"])
        try:
            value = await self._idempotent(session, "g2.clock.set_alarm", "alarm", payload)
            return _decode_clock_result(
                value,
                operation_id=operation_id,
                kind="alarm",
                local_time=local_time,
                expected_date=payload.get("date"),
                expected_repeat_days=payload.get("repeat_days", []),
                allow_resolved_date="date" not in payload and "repeat_days" not in payload,
            )
        except (RelayError, ValueError):
            return {"success": False, "state": "outcome_unknown", "error": "Clock outcome could not be confirmed"}

    async def _reminder_create(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"schedule", "text"})
        schedule = _safe_line(args["schedule"], max_chars=128, max_bytes=256)
        text = _safe_line(args["text"], max_chars=MAX_NOTIFY_CHARS, max_bytes=MAX_TOOL_TEXT_BYTES)
        operation_id = _stable_operation("rem", session["claims"])
        try:
            value = await self._idempotent(session, "g2.reminders.create", "rem", {"schedule": schedule, "text": text})
            return _decode_reminder_result(value, operation_id=operation_id)
        except (RelayError, ValueError):
            return {"success": False, "state": "outcome_unknown", "error": "Reminder outcome could not be confirmed"}

    async def _weather_present(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"location"}, {"day_offset", "date"})
        location = _safe_line(args["location"], max_chars=80, max_bytes=160)
        if "day_offset" in args and "date" in args:
            raise ToolInputError("day_offset and date are mutually exclusive")
        relay_args: dict[str, Any] = {"location": location}
        if "day_offset" in args:
            offset = args["day_offset"]
            if type(offset) is not int or not 0 <= offset <= 7:
                raise ToolInputError("day_offset must be an integer from 0 to 7")
            relay_args["day_offset"] = offset
        if "date" in args:
            value = args["date"]
            if not isinstance(value, str):
                raise ToolInputError("date must use YYYY-MM-DD")
            try:
                relay_args["date"] = Date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise ToolInputError("date must be a real YYYY-MM-DD date") from exc
        try:
            read = await self._relay(session, "g2.weather.read_forecast", relay_args)
            spec, key, title, primary, fresh = _weather_spec(read)
            intent = f"Weather for {location}"
            if "date" in relay_args:
                intent += f" on {relay_args['date']}"
            elif "day_offset" in relay_args:
                intent += f" day {relay_args['day_offset']}"
            else:
                intent += " today"
            operation_id = _stable_operation("weather", session["claims"])
            presented = await self._idempotent(session, "g2.context.present", "weather", {
                "intent": intent,
                "refresh_policy": {"mode": "on_visible" if fresh else "manual", "min_interval_seconds": 900},
                "regeneration": "self_contained_intent" if fresh else "current_turn_only",
                "spec": spec,
            })
            _decode_present_result(
                presented,
                operation_id=operation_id,
                dashboard_key=key,
            )
            return {"success": True, "dashboard_key": key, "title": title, "summary": primary}
        except (RelayError, ValueError, KeyError, TypeError):
            return {"success": False, "error": "Weather could not be presented safely"}

    async def _train_departures_present(self, arguments: Any, session: dict[str, Any]) -> dict[str, Any]:
        args = _exact_args(arguments, {"origin_crs", "destination_crs"})
        origin = args["origin_crs"]
        destination = args["destination_crs"]
        if (
            not isinstance(origin, str)
            or not isinstance(destination, str)
            or not _CRS.fullmatch(origin)
            or not _CRS.fullmatch(destination)
            or origin == destination
        ):
            raise ToolInputError("station codes must be distinct uppercase three-character CRS codes")
        try:
            read = await self._relay(session, "g2.transit.read_departures", {
                "origin_crs": origin, "destination_crs": destination,
            })
            spec, key, title, primary = _train_spec(read, origin, destination)
            operation_id = _stable_operation("train", session["claims"])
            presented = await self._idempotent(session, "g2.context.present", "train", {
                "intent": f"Train departures from {origin} to {destination}",
                "refresh_policy": {"mode": "on_visible", "min_interval_seconds": 60},
                "regeneration": "self_contained_intent",
                "spec": spec,
            })
            _decode_present_result(
                presented,
                operation_id=operation_id,
                dashboard_key=key,
            )
            return {
                "success": True,
                "dashboard_key": key,
                "title": title,
                "summary": primary,
            }
        except (RelayError, ValueError, KeyError, TypeError):
            return {"success": False, "error": "Train times could not be presented safely"}


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _one_of(*variants: dict[str, Any]) -> dict[str, Any]:
    # MCP's Tool inputSchema/outputSchema types require an explicit root
    # object declaration even when the alternatives are all object schemas.
    return {"type": "object", "oneOf": list(variants)}


_PUBLIC_FAILURE_SCHEMA = _object_schema({
    "success": {"const": False},
    "state": {"type": "string", "enum": ["not_committed", "outcome_unknown", "error"]},
    "error": {"type": "string", "minLength": 1, "maxLength": 240},
}, ["success", "state", "error"])

_BASE_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "g2_work_task_add": _one_of(
        _object_schema({
            "success": {"const": True},
            "state": {"const": "completed"},
            "status": {"type": "string", "enum": ["acknowledged", "historical_acknowledgement"]},
            "task_id": {"type": "string", "pattern": _WORK_TASK_ID.pattern},
            "lane": {"type": "string", "enum": ["inbox", "today", "doing"]},
            "board_revision": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
        }, ["success", "state", "status", "task_id", "lane", "board_revision"]),
        _PUBLIC_FAILURE_SCHEMA,
    ),
    "g2_clock_set_timer": _one_of(
        _object_schema({
            "success": {"const": True},
            "state": {"const": "completed"},
            "status": {"type": "string", "enum": ["acknowledged", "historical_acknowledgement"]},
            "item_id": {"type": "string", "pattern": _CLOCK_ITEM_ID.pattern},
            "kind": {"const": "timer"},
            "next_fire_at_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "clock_revision": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_CLOCK_DURATION_SECONDS},
        }, ["success", "state", "status", "item_id", "kind", "next_fire_at_ms", "clock_revision", "duration_seconds"]),
        _PUBLIC_FAILURE_SCHEMA,
    ),
    "g2_clock_set_alarm": _one_of(
        _object_schema({
            "success": {"const": True},
            "state": {"const": "completed"},
            "status": {"type": "string", "enum": ["acknowledged", "historical_acknowledgement"]},
            "item_id": {"type": "string", "pattern": _CLOCK_ITEM_ID.pattern},
            "kind": {"const": "alarm"},
            "next_fire_at_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "clock_revision": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "local_time": {"type": "string", "pattern": _CLOCK_TIME.pattern},
            "date": {"type": ["string", "null"]},
            "repeat_days": {"type": "array", "maxItems": 7, "uniqueItems": True,
                            "items": {"type": "string", "enum": list(_WEEKDAYS)}},
        }, ["success", "state", "status", "item_id", "kind", "next_fire_at_ms", "clock_revision", "local_time", "date", "repeat_days"]),
        _PUBLIC_FAILURE_SCHEMA,
    ),
    "g2_reminder_create": _one_of(
        _object_schema({
            "success": {"const": True},
            "state": {"const": "scheduled"},
            "status": {"type": "string", "enum": ["scheduled", "historical_scheduled"]},
            "reminder_id": {"type": "string", "pattern": _REMINDER_ID.pattern},
            "due_at": {"type": "string", "pattern": _INSTANT.pattern},
        }, ["success", "state", "status", "reminder_id", "due_at"]),
        _object_schema({
            "success": {"const": True},
            "state": {"const": "delivered"},
            "status": {"const": "historical_delivered"},
            "delivery_status": {"type": "string", "enum": ["queued", "acknowledged", "historical_acknowledgement"]},
        }, ["success", "state", "status", "delivery_status"]),
        _PUBLIC_FAILURE_SCHEMA,
    ),
    "g2_weather_present": _one_of(
        _object_schema({
            "success": {"const": True},
            "dashboard_key": {"type": "string", "pattern": r"^weather-[a-f0-9]{32}$|^weather-error$"},
            "title": {"type": "string", "minLength": 1, "maxLength": 48},
            "summary": {"type": "string", "minLength": 1, "maxLength": 240},
        }, ["success", "dashboard_key", "title", "summary"]),
        _object_schema({
            "success": {"const": False},
            "error": {"type": "string", "minLength": 1, "maxLength": 240},
        }, ["success", "error"]),
    ),
    "g2_train_departures_present": _one_of(
        _object_schema({
            "success": {"const": True},
            "dashboard_key": {"type": "string", "pattern": r"^rail-[a-f0-9]{24}$"},
            "title": {"type": "string", "minLength": 1, "maxLength": 48},
            "summary": {"type": "string", "minLength": 1, "maxLength": 240},
        }, ["success", "dashboard_key", "title", "summary"]),
        _object_schema({
            "success": {"const": False},
            "error": {"type": "string", "minLength": 1, "maxLength": 240},
        }, ["success", "error"]),
    ),
}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "g2_work_task_add",
        "description": "Add a day-job task to the wearer's local Work Tasks app, never Hermes Kanban or todo.",
        "inputSchema": _object_schema({
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "lane": {"type": "string", "enum": ["inbox", "today", "doing"], "default": "inbox"},
        }, ["title"]),
    },
    {
        "name": "g2_clock_set_timer",
        "description": "Set a phone-owned Clock countdown for timer intent; never use reminders or cron.",
        "inputSchema": _object_schema({
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_CLOCK_DURATION_SECONDS},
            "label": {"type": "string", "minLength": 1, "maxLength": 80},
        }, ["duration_seconds"]),
    },
    {
        "name": "g2_clock_set_alarm",
        "description": "Set a phone-owned Clock alarm for alarm or wake-me intent; never use reminders or cron.",
        "inputSchema": _object_schema({
            "local_time": {"type": "string", "pattern": _CLOCK_TIME.pattern},
            "date": {"type": "string", "format": "date"},
            "repeat_days": {"type": "array", "minItems": 1, "maxItems": 7, "uniqueItems": True,
                            "items": {"type": "string", "enum": list(_WEEKDAYS)}},
            "label": {"type": "string", "minLength": 1, "maxLength": 80},
        }, ["local_time"]),
    },
    {
        "name": "g2_reminder_create",
        "description": "Create one one-shot reminder for explicit remind-me intent; never use for timers or alarms.",
        "inputSchema": _object_schema({
            "schedule": {"type": "string", "minLength": 1, "maxLength": 128},
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_NOTIFY_CHARS},
        }, ["schedule", "text"]),
    },
    {
        "name": "g2_weather_present",
        "description": "Read typed UK public weather and atomically present the final attributed deck.",
        "inputSchema": _object_schema({
            "location": {"type": "string", "minLength": 1, "maxLength": 80},
            "day_offset": {"type": "integer", "minimum": 0, "maximum": 7},
            "date": {"type": "string", "format": "date"},
        }, ["location"]),
    },
    {
        "name": "g2_train_departures_present",
        "description": "Read typed National Rail departures and atomically present the final deck.",
        "inputSchema": _object_schema({
            "origin_crs": {"type": "string", "pattern": _CRS.pattern},
            "destination_crs": {"type": "string", "pattern": _CRS.pattern},
        }, ["origin_crs", "destination_crs"]),
    },
)


def _contracted_base_tool(tool: dict[str, Any]) -> dict[str, Any]:
    name = tool["name"]
    input_schema = dict(tool["inputSchema"])
    if name == "g2_clock_set_alarm":
        input_schema["not"] = {"required": ["date", "repeat_days"]}
    elif name == "g2_weather_present":
        input_schema["not"] = {"required": ["day_offset", "date"]}
    return {
        **tool,
        "inputSchema": input_schema,
        "outputSchema": _BASE_OUTPUT_SCHEMAS[name],
    }


TOOLS = tuple(_contracted_base_tool(tool) for tool in TOOLS) + DEVICE_TOOLS
_TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _generic_tool_failure(
    name: str,
    error: str,
    *,
    permission: bool = False,
) -> dict[str, Any]:
    """Return a bounded failure that conforms to this tool's output schema."""

    safe_error = _safe_line(error, max_chars=240, max_bytes=960)
    if name in DEVICE_TOOL_NAMES:
        return {
            "success": False,
            "state": "error",
            "error_code": "permission" if permission else "phone_error",
            "error": safe_error,
        }
    if name in {"g2_weather_present", "g2_train_departures_present"}:
        return {"success": False, "error": safe_error}
    return {"success": False, "state": "error", "error": safe_error}


def _tool_result(
    name: str,
    value: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    text = _json(value)
    if len(text.encode("utf-8")) > MAX_RESULT_TEXT_BYTES:
        value = _generic_tool_failure(
            name, "Workflow result exceeded the bounded response limit"
        )
        text = _json(value)
        is_error = True
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value,
        "isError": is_error,
    }


def _valid_request_id(value: Any) -> bool:
    if type(value) is int:
        return 0 <= value <= MAX_SAFE_INTEGER
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


class WorkflowMcpServer:
    """Small MCP stdio request handler compatible with Hermes' installed SDK."""

    def __init__(self, service: WorkflowService | None = None) -> None:
        self.service = service or WorkflowService()
        self.lifecycle = "new"
        self.protocol_version: str | None = None

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            request_id = message.get("id") if isinstance(message, dict) and _valid_request_id(message.get("id")) else None
            return _rpc_error(request_id, -32600, "Invalid Request")
        method = message["method"]
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and not _valid_request_id(request_id):
            return _rpc_error(None, -32600, "Invalid Request")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return None if not has_id else _rpc_error(request_id, -32602, "Invalid params")

        if not has_id:
            if (
                method == "notifications/initialized"
                and self.lifecycle == "initializing"
                and set(message) <= {"jsonrpc", "method", "params"}
                and params == {}
            ):
                self.lifecycle = "initialized"
            return None

        if method == "initialize":
            if self.lifecycle != "new":
                return _rpc_error(request_id, -32600, "Already initialized")
            offered = params.get("protocolVersion")
            if not isinstance(offered, str):
                return _rpc_error(request_id, -32602, "Invalid initialize parameters")
            negotiated = offered if offered in HANDSHAKE_VERSIONS else LATEST_HANDSHAKE_VERSION
            self.protocol_version = negotiated
            self.lifecycle = "initializing"
            return _rpc_result(request_id, {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use these intent-complete tools for G2 Work Tasks, Clock, reminders, "
                    "UK trains and UK public weather. Do not rebuild "
                    "these workflows with terminal, cron, browser, or arbitrary phone tools."
                ),
            })
        if method == "ping":
            return _rpc_result(request_id, {})
        if self.lifecycle != "initialized":
            return _rpc_error(request_id, -32002, "Server is not initialized")
        if method == "tools/list":
            if set(params) - {"cursor", "_meta"} or params.get("cursor") not in (None, ""):
                return _rpc_error(request_id, -32602, "Invalid tools/list parameters")
            return _rpc_result(request_id, {"tools": json.loads(_json(TOOLS))})
        if method == "tools/call":
            if set(params) - {"name", "arguments", "_meta"}:
                return _rpc_error(request_id, -32602, "Invalid tools/call parameters")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or name not in _TOOL_NAMES or not isinstance(arguments, dict):
                return _rpc_error(request_id, -32602, "Invalid tools/call parameters")
            try:
                value = await self.service.call_tool(name, arguments, params.get("_meta"))
                return _rpc_result(
                    request_id,
                    _tool_result(
                        name,
                        value,
                        is_error=value.get("success") is not True,
                    ),
                )
            except PermissionError:
                return _rpc_result(
                    request_id,
                    _tool_result(
                        name,
                        _generic_tool_failure(
                            name,
                            "This workflow requires a trusted active even-g2 capability",
                            permission=True,
                        ),
                        is_error=True,
                    ),
                )
            except ToolInputError as exc:
                return _rpc_result(
                    request_id,
                    _tool_result(
                        name,
                        _generic_tool_failure(name, str(exc)),
                        is_error=True,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return _rpc_result(
                    request_id,
                    _tool_result(
                        name,
                        _generic_tool_failure(
                            name, "The G2 workflow failed safely"
                        ),
                        is_error=True,
                    ),
                )
        return _rpc_error(request_id, -32601, f"Method not found: {method}")


def _request_key(value: Any) -> tuple[type, Any] | None:
    return (type(value), value) if _valid_request_id(value) else None


def _cancel_request_key(message: Any) -> tuple[type, Any] | None:
    """Validate one exact MCP cancellation notification without reflecting it."""
    if (
        not isinstance(message, dict)
        or set(message) != {"jsonrpc", "method", "params"}
        or message.get("jsonrpc") != "2.0"
        or message.get("method") != "notifications/cancelled"
    ):
        return None
    params = message.get("params")
    if (
        not isinstance(params, dict)
        or "requestId" not in params
        or set(params) - {"requestId", "reason"}
    ):
        return None
    if "reason" in params:
        try:
            _safe_line(params["reason"], max_chars=160, max_bytes=640)
        except ToolInputError:
            return None
    return _request_key(params.get("requestId"))


async def _write_message(message: dict[str, Any], lock: asyncio.Lock) -> None:
    encoded = (_json(message) + "\n").encode("utf-8")
    if len(encoded) > MAX_BYTES:
        request_id = message.get("id") if _valid_request_id(message.get("id")) else None
        encoded = (_json(_rpc_error(request_id, -32603, "Response exceeded the bounded message limit")) + "\n").encode("utf-8")
    async with lock:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


async def _serve_stdio(server: WorkflowMcpServer) -> None:
    """Run newline-delimited MCP while allowing standard request cancellation."""
    pending: dict[tuple[type, Any], asyncio.Task[None]] = {}
    write_lock = asyncio.Lock()

    async def dispatch(message: dict[str, Any], key: tuple[type, Any]) -> None:
        try:
            response = await server.handle_message(message)
            if response is not None:
                await _write_message(response, write_lock)
        except asyncio.CancelledError:
            pass
        finally:
            pending.pop(key, None)

    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_BYTES + 2)
        if not raw:
            break
        if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
            if not raw.endswith(b"\n"):
                while True:
                    tail = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_BYTES + 2)
                    if not tail or tail.endswith(b"\n"):
                        break
            await _write_message(_rpc_error(None, -32700, "Parse error"), write_lock)
            continue
        try:
            message = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeError, ValueError):
            await _write_message(_rpc_error(None, -32700, "Parse error"), write_lock)
            continue

        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/cancelled"
            and "id" not in message
        ):
            key = _cancel_request_key(message)
            task = pending.get(key) if key is not None else None
            if task is not None:
                task.cancel()
            continue
        key = _request_key(message.get("id")) if isinstance(message, dict) and "id" in message else None
        if isinstance(message, dict) and message.get("method") == "tools/call" and key is not None:
            if key in pending:
                await _write_message(_rpc_error(message["id"], -32600, "Duplicate request id"), write_lock)
                continue
            task = asyncio.create_task(dispatch(message, key))
            pending[key] = task
            continue
        response = await server.handle_message(message)
        if response is not None:
            await _write_message(response, write_lock)

    tasks = list(pending.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _main() -> None:
    await _serve_stdio(WorkflowMcpServer())


if __name__ == "__main__":
    asyncio.run(_main())
