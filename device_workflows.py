"""Static, intent-level Device MCP workflows for the public model surface.

This module contains no phone tool discovery or arbitrary call primitive.  It
validates one of six closed user intents and maps it to one fixed native relay
workflow.  The native bridge, not this process, owns the raw phone MCP names,
schema pins, active-turn revalidation, and privacy reduction.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any


MAX_DEVICE_RESULT_BYTES = 24 * 1024
_WINDOW_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_BIDI_CONTROLS = frozenset({
    0x061C,
    0x200E,
    0x200F,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
})

LAUNCHABLE_APP_IDS = (
    "agent-cockpit",
    "blocks",
    "calendar",
    "clock",
    "compass",
    "conversate",
    "evenhub-local-counter",
    "files",
    "freecell",
    "minesweeper",
    "music",
    "navigate",
    "notifications",
    "pinball",
    "settings",
    "universal-search",
    "weather",
    "work-tasks",
)


class DeviceInputError(ValueError):
    """A model-authored device intent did not match the closed schema."""


class DeviceOutputError(ValueError):
    """The native bridge returned a malformed typed device result."""


@dataclass(frozen=True)
class DeviceInvocation:
    workflow: str
    arguments: dict[str, Any]
    mutating: bool


def _object_schema(
    properties: dict[str, Any],
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _variant(
    action: str,
    properties: dict[str, Any] | None = None,
    *,
    optional: tuple[str, ...] = (),
) -> dict[str, Any]:
    fields = {"action": {"const": action}}
    fields.update(properties or {})
    return _object_schema(fields, tuple(key for key in fields if key not in optional))


_APP_ID_SCHEMA = {"type": "string", "enum": list(LAUNCHABLE_APP_IDS)}
_WINDOW_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": _WINDOW_ID.pattern,
}
_FOLDER_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 24}

_FAILURE_OUTPUT = _object_schema(
    {
        "success": {"const": False},
        "state": {"const": "error"},
        "error_code": {
            "type": "string",
            "enum": [
                "permission",
                "contract_drift",
                "phone_error",
                "unavailable",
                "outcome_unknown",
            ],
        },
        "error": {"type": "string", "minLength": 1, "maxLength": 240},
    },
    ("success", "state", "error_code", "error"),
)
_COMPLETED_OUTPUT = _object_schema(
    {"success": {"const": True}, "state": {"const": "completed"}},
    ("success", "state"),
)
_WINDOW_OUTPUT = _object_schema(
    {
        "window_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "title": {"type": "string", "maxLength": 160},
        "app_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "foreground": {"type": "boolean"},
        "pinned": {"type": "boolean"},
    },
    ("window_id", "title", "app_id", "foreground", "pinned"),
)
_WINDOWS_OUTPUT = _object_schema(
    {
        "success": {"const": True},
        "state": {"type": "string", "enum": ["empty", "available"]},
        "windows": {"type": "array", "maxItems": 32, "items": _WINDOW_OUTPUT},
    },
    ("success", "state", "windows"),
)
_FOLDER_OUTPUT = _object_schema(
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 24},
        "app_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": len(LAUNCHABLE_APP_IDS),
            "items": {"type": "string", "enum": list(LAUNCHABLE_APP_IDS)},
        },
    },
    ("name", "app_ids"),
)
_FOLDERS_OUTPUT = _object_schema(
    {
        "success": {"const": True},
        "state": {"type": "string", "enum": ["empty", "available"]},
        "folders": {"type": "array", "maxItems": 32, "items": _FOLDER_OUTPUT},
        "ungrouped_app_ids": {
            "type": "array",
            "maxItems": len(LAUNCHABLE_APP_IDS),
            "items": {"type": "string", "enum": list(LAUNCHABLE_APP_IDS)},
        },
    },
    ("success", "state", "folders", "ungrouped_app_ids"),
)
def _summary_output(states: tuple[str, ...]) -> dict[str, Any]:
    return _object_schema(
        {
            "success": {"const": True},
            "state": {"type": "string", "enum": list(states)},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2_000},
        },
        ("success", "state", "summary"),
    )


_MEDIA_SUMMARY_OUTPUT = _summary_output(("idle", "unavailable", "playing", "paused"))
_NAVIGATION_SUMMARY_OUTPUT = _summary_output(("inactive", "stopped", "arrived", "active"))
_NOTIFICATION_OUTPUT = _object_schema(
    {
        "key": {"type": "string", "minLength": 1, "maxLength": 512},
        "app": {"type": "string", "minLength": 1, "maxLength": 120},
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    ("key", "app", "summary"),
)
_NOTIFICATIONS_OUTPUT = _object_schema(
    {
        "success": {"const": True},
        "state": {"type": "string", "enum": ["empty", "available"]},
        "notifications": {
            "type": "array",
            "maxItems": 20,
            "items": _NOTIFICATION_OUTPUT,
        },
    },
    ("success", "state", "notifications"),
)


def _nullable_number(minimum: float, maximum: float) -> dict[str, Any]:
    return {
        "oneOf": [
            {"type": "null"},
            {"type": "number", "minimum": minimum, "maximum": maximum},
        ]
    }


_HEALTH_LATEST_OUTPUT = _object_schema(
    {
        "date": {"type": "string", "format": "date"},
        "steps": _nullable_number(0, 1_000_000),
        "resting_hr_bpm": _nullable_number(20, 300),
        "hrv_ms": _nullable_number(0, 1_000),
        "spo2_percent": _nullable_number(0, 100),
        "sleep_score": _nullable_number(0, 100),
        "sleep_minutes": _nullable_number(0, 1_440),
        "readiness_score": _nullable_number(0, 100),
    },
    (
        "date",
        "steps",
        "resting_hr_bpm",
        "hrv_ms",
        "spo2_percent",
        "sleep_score",
        "sleep_minutes",
        "readiness_score",
    ),
)
_HEALTH_RANGE_OUTPUT = _object_schema(
    {
        "start_date": {"type": "string", "format": "date"},
        "end_date": {"type": "string", "format": "date"},
    },
    ("start_date", "end_date"),
)
_ACTIVITY_OUTPUT = _object_schema(
    {
        "date": {"type": "string", "format": "date"},
        "steps": _nullable_number(0, 1_000_000),
        "active_calories": _nullable_number(0, 100_000),
        "total_calories": _nullable_number(0, 100_000),
    },
    ("date", "steps", "active_calories", "total_calories"),
)
_HEALTH_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"const": True},
        "state": {"type": "string", "enum": ["empty", "available"]},
        "range": _HEALTH_RANGE_OUTPUT,
        "days_with_data": {"type": "integer", "minimum": 0, "maximum": 31},
        "latest": {"oneOf": [{"type": "null"}, _HEALTH_LATEST_OUTPUT]},
        "activity_today": _ACTIVITY_OUTPUT,
    },
    "required": ["success", "state", "range", "days_with_data", "latest"],
    "additionalProperties": False,
}
_CALENDAR_OUTPUT = _object_schema(
    {
        "success": {"const": True},
        "state": {"type": "string", "enum": ["empty", "available"]},
        "events": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 600},
        },
    },
    ("success", "state", "events"),
)

DEVICE_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "g2_apps_manage",
        "description": (
            "Open apps, inspect or manage windows, and inspect or manage launcher "
            "folders on the wearer's G2. Choose exactly one action."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                _variant("launch", {"app_id": _APP_ID_SCHEMA}),
                _variant("list_windows"),
                _variant("focus_window", {"window_id": _WINDOW_ID_SCHEMA}),
                _variant("close_window", {"window_id": _WINDOW_ID_SCHEMA}),
                _variant("list_folders"),
                _variant(
                    "move_to_folder",
                    {"app_id": _APP_ID_SCHEMA, "folder": _FOLDER_SCHEMA},
                ),
                _variant("remove_from_folder", {"app_id": _APP_ID_SCHEMA}),
                _variant("disband_folder", {"folder": _FOLDER_SCHEMA}),
            ]
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _COMPLETED_OUTPUT, _WINDOWS_OUTPUT, _FOLDERS_OUTPUT]
        },
    },
    {
        "name": "g2_media_control",
        "description": (
            "Read current phone media status, toggle play/pause, or skip to the next track."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                _variant("status"),
                _variant("play_pause"),
                _variant("next"),
            ]
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _COMPLETED_OUTPUT, _MEDIA_SUMMARY_OUTPUT]
        },
    },
    {
        "name": "g2_navigation",
        "description": (
            "Start, stop, or read G2 turn-by-turn navigation. Starting accepts one "
            "bounded destination and optional travel profile."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                _variant(
                    "start",
                    {
                        "destination": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                        },
                        "profile": {
                            "type": "string",
                            "enum": ["driving", "walking", "cycling"],
                        },
                    },
                    optional=("profile",),
                ),
                _variant("stop"),
                _variant("status"),
            ]
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _NAVIGATION_SUMMARY_OUTPUT],
        },
    },
    {
        "name": "g2_notifications",
        "description": (
            "List a bounded set of current phone notifications or dismiss one exact "
            "notification key returned by this tool."
        ),
        "inputSchema": {
            "type": "object",
            "oneOf": [
                _variant(
                    "list",
                    {"max": {"type": "integer", "minimum": 1, "maximum": 20}},
                    optional=("max",),
                ),
                _variant(
                    "dismiss",
                    {"key": {"type": "string", "minLength": 1, "maxLength": 512}},
                ),
            ]
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _COMPLETED_OUTPUT, _NOTIFICATIONS_OUTPUT]
        },
    },
    {
        "name": "g2_health_summary",
        "description": (
            "Read a coarse local ring-health summary for at most 31 days. Never returns "
            "hourly samples; phone consent and the exact active turn are required."
        ),
        "inputSchema": _object_schema(
            {
                "days": {"type": "integer", "minimum": 1, "maximum": 31},
                "end_date": {"type": "string", "format": "date"},
            },
            (),
        ),
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _HEALTH_OUTPUT],
        },
    },
    {
        "name": "g2_calendar_agenda",
        "description": (
            "Read a bounded upcoming agenda from the phone calendar for the active G2 turn."
        ),
        "inputSchema": _object_schema(
            {
                "within_hours": {"type": "integer", "minimum": 1, "maximum": 720},
                "max_events": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            (),
        ),
        "outputSchema": {
            "type": "object",
            "oneOf": [_FAILURE_OUTPUT, _CALENDAR_OUTPUT],
        },
    },
)

DEVICE_TOOL_NAMES = frozenset(tool["name"] for tool in DEVICE_TOOLS)

_INTERNAL_WORKFLOWS = {
    "g2_apps_manage": "g2.device.apps.manage",
    "g2_media_control": "g2.device.media.control",
    "g2_navigation": "g2.device.navigation",
    "g2_notifications": "g2.device.notifications",
    "g2_health_summary": "g2.device.health.summary",
    "g2_calendar_agenda": "g2.device.calendar.agenda",
}


def _exact(arguments: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DeviceInputError("Device workflow arguments must be an object")
    optional = optional or set()
    if not required <= set(arguments) or set(arguments) - required - optional:
        raise DeviceInputError("Device workflow arguments do not match the selected action")
    return dict(arguments)


def _safe_line(value: Any, *, max_chars: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise DeviceInputError("Expected one bounded text value")
    try:
        text = unicodedata.normalize("NFC", value).strip()
        encoded = text.encode("utf-8")
    except (TypeError, UnicodeError) as exc:
        raise DeviceInputError("Expected one bounded text value") from exc
    if not text or len(text) > max_chars or len(encoded) > max_bytes:
        raise DeviceInputError("Expected one bounded text value")
    for character in text:
        codepoint = ord(character)
        if (
            codepoint in _BIDI_CONTROLS
            or 0xD800 <= codepoint <= 0xDFFF
            or unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
        ):
            raise DeviceInputError("Expected one inert text line")
    return text


def _bounded_integer(value: Any, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DeviceInputError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def normalize_device_invocation(name: str, arguments: Any) -> DeviceInvocation:
    workflow = _INTERNAL_WORKFLOWS.get(name)
    if workflow is None:
        raise DeviceInputError("Unknown device workflow")

    if name == "g2_apps_manage":
        base = _exact(arguments, {"action"}, {"app_id", "window_id", "folder"})
        action = base.get("action")
        shapes = {
            "launch": ({"action", "app_id"}, True),
            "list_windows": ({"action"}, False),
            "focus_window": ({"action", "window_id"}, True),
            "close_window": ({"action", "window_id"}, True),
            "list_folders": ({"action"}, False),
            "move_to_folder": ({"action", "app_id", "folder"}, True),
            "remove_from_folder": ({"action", "app_id"}, True),
            "disband_folder": ({"action", "folder"}, True),
        }
        selected = shapes.get(action)
        if selected is None or set(base) != selected[0]:
            raise DeviceInputError("Apps arguments do not match the selected action")
        normalized: dict[str, Any] = {"action": action}
        if "app_id" in base:
            if base["app_id"] not in LAUNCHABLE_APP_IDS:
                raise DeviceInputError("app_id is not in the reviewed launcher set")
            normalized["app_id"] = base["app_id"]
        if "window_id" in base:
            window_id = base["window_id"]
            if not isinstance(window_id, str) or _WINDOW_ID.fullmatch(window_id) is None:
                raise DeviceInputError("window_id does not match the bounded G2 identifier format")
            normalized["window_id"] = window_id
        if "folder" in base:
            normalized["folder"] = _safe_line(base["folder"], max_chars=24, max_bytes=96)
        return DeviceInvocation(workflow, normalized, selected[1])

    if name == "g2_media_control":
        base = _exact(arguments, {"action"})
        if base["action"] not in {"status", "play_pause", "next"}:
            raise DeviceInputError("Unknown media action")
        return DeviceInvocation(workflow, base, base["action"] != "status")

    if name == "g2_navigation":
        base = _exact(arguments, {"action"}, {"destination", "profile"})
        action = base["action"]
        if action == "start":
            if set(base) - {"action", "destination", "profile"} or "destination" not in base:
                raise DeviceInputError("Navigation start requires one destination")
            normalized = {
                "action": "start",
                "destination": _safe_line(base["destination"], max_chars=160, max_bytes=480),
            }
            if "profile" in base:
                if base["profile"] not in {"driving", "walking", "cycling"}:
                    raise DeviceInputError("Unknown navigation profile")
                normalized["profile"] = base["profile"]
            return DeviceInvocation(workflow, normalized, True)
        if action not in {"stop", "status"} or set(base) != {"action"}:
            raise DeviceInputError("Navigation arguments do not match the selected action")
        return DeviceInvocation(workflow, base, action == "stop")

    if name == "g2_notifications":
        base = _exact(arguments, {"action"}, {"max", "key"})
        action = base["action"]
        if action == "list":
            if set(base) - {"action", "max"}:
                raise DeviceInputError("Notification list accepts only max")
            maximum = _bounded_integer(base.get("max", 10), 1, 20, "max")
            return DeviceInvocation(workflow, {"action": "list", "max": maximum}, False)
        if action == "dismiss" and set(base) == {"action", "key"}:
            key = _safe_line(base["key"], max_chars=512, max_bytes=2_048)
            return DeviceInvocation(workflow, {"action": "dismiss", "key": key}, True)
        raise DeviceInputError("Notification arguments do not match the selected action")

    if name == "g2_health_summary":
        base = _exact(arguments, set(), {"days", "end_date"})
        normalized = {"days": _bounded_integer(base.get("days", 7), 1, 31, "days")}
        if "end_date" in base:
            value = base["end_date"]
            if not isinstance(value, str) or _DATE.fullmatch(value) is None:
                raise DeviceInputError("end_date must be a real YYYY-MM-DD date")
            try:
                normalized["end_date"] = date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise DeviceInputError("end_date must be a real YYYY-MM-DD date") from exc
        return DeviceInvocation(workflow, normalized, False)

    base = _exact(arguments, set(), {"within_hours", "max_events"})
    return DeviceInvocation(
        workflow,
        {
            "within_hours": _bounded_integer(
                base.get("within_hours", 168), 1, 720, "within_hours"
            ),
            "max_events": _bounded_integer(base.get("max_events", 10), 1, 20, "max_events"),
        },
        False,
    )


def _output_line(value: Any, *, max_chars: int, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    try:
        normalized = _safe_line(value, max_chars=max_chars, max_bytes=max_chars * 4)
    except DeviceInputError as exc:
        raise DeviceOutputError("Device workflow text is malformed") from exc
    if normalized != value:
        raise DeviceOutputError("Device workflow text is not canonical")
    return normalized


def _output_date(value: Any) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise DeviceOutputError("Device workflow date is malformed")
    try:
        parsed = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DeviceOutputError("Device workflow date is malformed") from exc
    if parsed != value:
        raise DeviceOutputError("Device workflow date is not canonical")
    return value


def _output_number(value: Any, minimum: float, maximum: float) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeviceOutputError("Device workflow number is malformed")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise DeviceOutputError("Device workflow number is malformed") from exc
    if number != number or number in {float("inf"), float("-inf")} or not minimum <= number <= maximum:
        raise DeviceOutputError("Device workflow number is outside bounds")


def _validate_apps_result(value: dict[str, Any]) -> None:
    state = value.get("state")
    if state == "completed" and set(value) == {"success", "state"}:
        return
    if set(value) == {"success", "state", "windows"} and state in {"empty", "available"}:
        windows = value.get("windows")
        if not isinstance(windows, list) or len(windows) > 32 or (state == "empty") != (not windows):
            raise DeviceOutputError("Device windows result is malformed")
        for window in windows:
            if not isinstance(window, dict) or set(window) != {
                "window_id", "title", "app_id", "foreground", "pinned"
            }:
                raise DeviceOutputError("Device window entry is malformed")
            if not isinstance(window["window_id"], str) or _WINDOW_ID.fullmatch(window["window_id"]) is None:
                raise DeviceOutputError("Device window identifier is malformed")
            _output_line(window["title"], max_chars=160, allow_empty=True)
            app_id = window["app_id"]
            if not isinstance(app_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", app_id) is None:
                raise DeviceOutputError("Device window app is malformed")
            if type(window["foreground"]) is not bool or type(window["pinned"]) is not bool:
                raise DeviceOutputError("Device window flags are malformed")
        return
    if set(value) == {"success", "state", "folders", "ungrouped_app_ids"} and state in {"empty", "available"}:
        folders = value.get("folders")
        ungrouped = value.get("ungrouped_app_ids")
        if (
            not isinstance(folders, list)
            or len(folders) > 32
            or not isinstance(ungrouped, list)
            or len(ungrouped) > len(LAUNCHABLE_APP_IDS)
            or (state == "empty") != (not folders)
        ):
            raise DeviceOutputError("Device folders result is malformed")
        seen_apps: set[str] = set()
        for folder in folders:
            if not isinstance(folder, dict) or set(folder) != {"name", "app_ids"}:
                raise DeviceOutputError("Device folder entry is malformed")
            _output_line(folder["name"], max_chars=24)
            members = folder["app_ids"]
            if not isinstance(members, list) or not members or len(members) > len(LAUNCHABLE_APP_IDS):
                raise DeviceOutputError("Device folder members are malformed")
            for app_id in members:
                if app_id not in LAUNCHABLE_APP_IDS or app_id in seen_apps:
                    raise DeviceOutputError("Device folder member is unreviewed or duplicated")
                seen_apps.add(app_id)
        for app_id in ungrouped:
            if app_id not in LAUNCHABLE_APP_IDS or app_id in seen_apps:
                raise DeviceOutputError("Device ungrouped app is unreviewed or duplicated")
            seen_apps.add(app_id)
        return
    raise DeviceOutputError("Apps workflow success is malformed")


def _validate_summary_result(value: dict[str, Any], states: set[str]) -> None:
    if set(value) != {"success", "state", "summary"} or value.get("state") not in states:
        raise DeviceOutputError("Device summary result is malformed")
    _output_line(value.get("summary"), max_chars=2_000)


def _validate_notifications_result(value: dict[str, Any]) -> None:
    if value.get("state") == "completed" and set(value) == {"success", "state"}:
        return
    if set(value) != {"success", "state", "notifications"} or value.get("state") not in {"empty", "available"}:
        raise DeviceOutputError("Notifications workflow success is malformed")
    items = value.get("notifications")
    if not isinstance(items, list) or len(items) > 20 or (value["state"] == "empty") != (not items):
        raise DeviceOutputError("Notification list is malformed")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "app", "summary"}:
            raise DeviceOutputError("Notification entry is malformed")
        key = _output_line(item["key"], max_chars=512)
        if key in seen:
            raise DeviceOutputError("Notification key is duplicated")
        seen.add(key)
        _output_line(item["app"], max_chars=120)
        _output_line(item["summary"], max_chars=500)


def _validate_health_result(value: dict[str, Any]) -> None:
    required = {"success", "state", "range", "days_with_data", "latest"}
    if not required <= set(value) or set(value) - required - {"activity_today"}:
        raise DeviceOutputError("Health workflow success is malformed")
    if value.get("state") not in {"empty", "available"}:
        raise DeviceOutputError("Health workflow state is malformed")
    range_value = value.get("range")
    if not isinstance(range_value, dict) or set(range_value) != {"start_date", "end_date"}:
        raise DeviceOutputError("Health range is malformed")
    start = _output_date(range_value["start_date"])
    end = _output_date(range_value["end_date"])
    days = value.get("days_with_data")
    if start > end or type(days) is not int or not 0 <= days <= 31:
        raise DeviceOutputError("Health range or count is malformed")
    latest = value.get("latest")
    latest_fields = {
        "date", "steps", "resting_hr_bpm", "hrv_ms", "spo2_percent",
        "sleep_score", "sleep_minutes", "readiness_score",
    }
    if latest is None:
        if value["state"] != "empty" or days != 0:
            raise DeviceOutputError("Empty health result is inconsistent")
    else:
        if value["state"] != "available" or days < 1 or not isinstance(latest, dict) or set(latest) != latest_fields:
            raise DeviceOutputError("Latest health summary is malformed")
        _output_date(latest["date"])
        for field, bounds in {
            "steps": (0, 1_000_000),
            "resting_hr_bpm": (20, 300),
            "hrv_ms": (0, 1_000),
            "spo2_percent": (0, 100),
            "sleep_score": (0, 100),
            "sleep_minutes": (0, 1_440),
            "readiness_score": (0, 100),
        }.items():
            _output_number(latest[field], *bounds)
    activity = value.get("activity_today")
    if activity is not None:
        if not isinstance(activity, dict) or set(activity) != {
            "date", "steps", "active_calories", "total_calories"
        }:
            raise DeviceOutputError("Health activity summary is malformed")
        _output_date(activity["date"])
        _output_number(activity["steps"], 0, 1_000_000)
        _output_number(activity["active_calories"], 0, 100_000)
        _output_number(activity["total_calories"], 0, 100_000)


def _validate_calendar_result(value: dict[str, Any]) -> None:
    if set(value) != {"success", "state", "events"} or value.get("state") not in {"empty", "available"}:
        raise DeviceOutputError("Calendar workflow success is malformed")
    events = value.get("events")
    if not isinstance(events, list) or len(events) > 20 or (value["state"] == "empty") != (not events):
        raise DeviceOutputError("Calendar event list is malformed")
    for event in events:
        _output_line(event, max_chars=600)


def validate_device_result(name: str, value: Any) -> dict[str, Any]:
    """Reject raw MCP envelopes and validate one exact coarse receipt."""
    if name not in DEVICE_TOOL_NAMES or not isinstance(value, dict):
        raise DeviceOutputError("Device workflow result is malformed")
    try:
        detached = json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise DeviceOutputError("Device workflow result is not strict JSON") from exc
    encoded = json.dumps(
        detached, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_DEVICE_RESULT_BYTES:
        raise DeviceOutputError("Device workflow result exceeded the bounded response limit")
    if "content" in detached or "isError" in detached or "tool" in detached:
        raise DeviceOutputError("Raw phone MCP results cannot cross the workflow boundary")
    if detached.get("success") is False:
        if set(detached) != {"success", "state", "error_code", "error"}:
            raise DeviceOutputError("Device workflow failure is malformed")
        if detached.get("state") != "error" or detached.get("error_code") not in {
            "permission", "contract_drift", "phone_error", "unavailable", "outcome_unknown"
        }:
            raise DeviceOutputError("Device workflow failure is malformed")
        _output_line(detached.get("error"), max_chars=240)
        return detached
    if detached.get("success") is not True or not isinstance(detached.get("state"), str):
        raise DeviceOutputError("Device workflow success is malformed")
    if name == "g2_apps_manage":
        _validate_apps_result(detached)
    elif name == "g2_media_control":
        if detached.get("state") == "completed" and set(detached) == {"success", "state"}:
            pass
        else:
            _validate_summary_result(detached, {"idle", "unavailable", "playing", "paused"})
    elif name == "g2_navigation":
        _validate_summary_result(detached, {"inactive", "stopped", "arrived", "active"})
    elif name == "g2_notifications":
        _validate_notifications_result(detached)
    elif name == "g2_health_summary":
        _validate_health_result(detached)
    elif name == "g2_calendar_agenda":
        _validate_calendar_result(detached)
    else:
        raise DeviceOutputError("Unknown device workflow result")
    return detached
