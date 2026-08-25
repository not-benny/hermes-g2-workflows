from __future__ import annotations

import json
import unittest
from typing import Any

import server


async def _initialized(service: Any) -> server.WorkflowMcpServer:
    instance = server.WorkflowMcpServer(service)
    await instance.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "contract-test", "version": "1"},
        },
    })
    await instance.handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized"
    })
    return instance


async def _call(instance: server.WorkflowMcpServer, name: str) -> dict[str, Any]:
    response = await instance.handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    })
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    return result["structuredContent"]


def _assert_failure_shape(
    case: unittest.TestCase,
    name: str,
    value: dict[str, Any],
    *,
    permission: bool,
) -> None:
    if name in server.DEVICE_TOOL_NAMES:
        case.assertEqual(set(value), {"success", "state", "error_code", "error"})
        case.assertIs(value["success"], False)
        case.assertEqual(value["state"], "error")
        case.assertEqual(
            value["error_code"], "permission" if permission else "phone_error"
        )
    elif name in {"g2_weather_present", "g2_train_departures_present"}:
        case.assertEqual(set(value), {"success", "error"})
        case.assertIs(value["success"], False)
    else:
        case.assertEqual(set(value), {"success", "state", "error"})
        case.assertIs(value["success"], False)
        case.assertEqual(value["state"], "error")
    case.assertIsInstance(value["error"], str)
    case.assertTrue(1 <= len(value["error"]) <= 240)


class _RaisingService:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def call_tool(self, _name: str, _arguments: Any, _meta: Any) -> dict[str, Any]:
        raise self.error


class _OversizedService:
    async def call_tool(self, _name: str, _arguments: Any, _meta: Any) -> dict[str, Any]:
        return {"success": True, "payload": "x" * server.MAX_RESULT_TEXT_BYTES}


class OutputContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_generic_error_conforms_to_its_public_failure_variant(self) -> None:
        cases = (
            (PermissionError("untrusted"), True),
            (server.ToolInputError("invalid request"), False),
            (RuntimeError("private detail"), False),
        )
        for error, permission in cases:
            instance = await _initialized(_RaisingService(error))
            for name in sorted(server._TOOL_NAMES):
                with self.subTest(name=name, error=type(error).__name__):
                    value = await _call(instance, name)
                    _assert_failure_shape(
                        self, name, value, permission=permission
                    )

    async def test_bounded_result_fallback_is_also_tool_specific(self) -> None:
        instance = await _initialized(_OversizedService())
        for name in sorted(server._TOOL_NAMES):
            with self.subTest(name=name):
                value = await _call(instance, name)
                _assert_failure_shape(self, name, value, permission=False)


if __name__ == "__main__":
    unittest.main()
