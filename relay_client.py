"""Capability-preserving client for the native bridge's fixed Unix relay."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any


MAX_BYTES = 64 * 1024


class RelayError(RuntimeError):
    pass


def _socket_path() -> Path:
    raw = os.environ.get("HERMES_G2_WORKFLOW_RELAY")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RelayError("G2 workflow relay endpoint is unavailable")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.name != "g2-workflows.sock":
        raise RelayError("G2 workflow relay endpoint is malformed")
    return candidate.resolve(strict=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


async def call_relay(
    tool: str,
    arguments: dict[str, Any],
    authorization: dict[str, Any],
    *,
    timeout: float = 190.0,
) -> str:
    if not isinstance(authorization, dict) or set(authorization) != {
        "capability",
        "workflow",
        "workflow_arguments",
        "subcall_id",
        "attempt",
    }:
        raise RelayError("G2 workflow authorization is malformed")
    socket_path = _socket_path()
    request_id = secrets.token_hex(16)
    try:
        payload = json.dumps(
            {
                "version": 1,
                "id": request_id,
                **authorization,
                "tool": tool,
                "arguments": arguments,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RelayError("G2 workflow request is not strict JSON") from exc
    if len(payload) > MAX_BYTES:
        raise RelayError("G2 workflow request is too large")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=3.0
        )
        try:
            writer.write(payload + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
    except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
        raise RelayError("G2 workflow relay did not return a result") from exc
    if not raw or len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise RelayError("G2 workflow relay returned an invalid result")
    try:
        response = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError) as exc:
        raise RelayError("G2 workflow relay returned an invalid result") from exc
    if (
        not isinstance(response, dict)
        or type(response.get("version")) is not int
        or response.get("version") != 1
        or response.get("id") != request_id
        or set(response) not in ({"version", "id", "ok", "result"}, {"version", "id", "ok", "error"})
    ):
        raise RelayError("G2 workflow relay returned an invalid result")
    if response.get("ok") is not True or not isinstance(response.get("result"), str):
        raise RelayError("G2 workflow relay rejected the request")
    return response["result"]
