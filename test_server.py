from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

import server


def session_meta(
    workflow: str,
    arguments: dict[str, Any],
    tool_call_id: str = "tool-call-1",
) -> dict[str, Any]:
    digest = hashlib.sha256(server._json(arguments).encode("utf-8")).hexdigest()
    return {
        server.CAPABILITY_META_KEY: {
            "claims": {
                "version": 1,
                "audience": server.CAPABILITY_AUDIENCE,
                "binding": "hermes-g2-workflows:workflows",
                "package_digest": "sha256:" + "b" * 64,
                "platform": "g2",
                "chat_id": "glasses",
                "message_id": "g2-turn-7-t1",
                "session_id": "session-even-g2-1",
                "profile": "even-g2",
                "tool_call_id": tool_call_id,
                "workflow": workflow,
                "arguments_sha256": digest,
                "issued_at": 1,
                "expires_at": 2,
                "nonce": "c" * 32,
            },
            "signature": "a" * 64,
        }
    }


class FakeRelay:
    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.responses = {name: list(values) for name, values in (responses or {}).items()}
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, name: str, arguments: dict[str, Any], session: dict[str, Any]) -> str:
        self.calls.append((name, copy.deepcopy(arguments), copy.deepcopy(session)))
        queue = self.responses.get(name)
        if not queue:
            raise AssertionError(f"unexpected relay call: {name}")
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(name, arguments, session)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def work_receipt(status: str):
    def make(_name, arguments, _authorization):
        return {
            "success": True,
            "receipt": {
                "status": status,
                "operation_id": arguments["operation_id"],
                "task_id": "wt_" + "1" * 32,
                "lane": arguments["lane"],
                "board_revision": 7,
            },
        }
    return make


def kanban_receipt(status: str = "acknowledged"):
    def make(_name, arguments, _authorization):
        return {
            "success": True,
            "receipt": {
                "status": status,
                "operation_id": arguments["operation_id"],
                "task_id": "t_1234abcd",
                "created_status": "blocked",
                "created_assignee": None,
                "board": "hermes-g2",
            },
        }
    return make


def timer_receipt(status: str):
    def make(_name, arguments, _authorization):
        return {
            "success": True,
            "receipt": {
                "status": status,
                "operation_id": arguments["operation_id"],
                "item_id": "clk_" + "2" * 32,
                "kind": "timer",
                "next_fire_at_ms": 1_787_702_999_000,
                "clock_revision": 9,
                "duration_seconds": arguments["duration_seconds"],
            },
        }
    return make


def present_receipt(
    status: str = "acknowledged",
    **overrides: Any,
):
    def make(_name, arguments, _authorization):
        receipt = {
            "status": status,
            "operation_id": arguments["operation_id"],
            "dashboard_key": arguments["spec"]["dashboard_key"],
            "dashboard_id": "ctx_" + "3" * 32,
            "presentation_generation": 1,
            "refresh_generation": 1,
            "revision": 1,
            "frame_id": 41,
        }
        receipt.update(overrides)
        return {"success": True, "receipt": receipt}
    return make


def present_blocked(error_code: str):
    messages = {
        "clock_alert_active": (
            "The glasses display is busy with an active Clock alert."
        ),
        "assistant_presentation_active": (
            "The glasses display is busy with another assistant presentation."
        ),
    }

    def make(_name, arguments, _authorization):
        return {
            "success": False,
            "commit_state": "not_committed",
            "operation_id": arguments["operation_id"],
            "error_code": error_code,
            "error": messages[error_code],
        }
    return make


async def initialized_server(relay: FakeRelay) -> server.WorkflowMcpServer:
    instance = server.WorkflowMcpServer(server.WorkflowService(relay))
    response = await instance.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        },
    })
    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-11-25"
    await instance.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return instance


async def call_tool(
    instance: server.WorkflowMcpServer,
    name: str,
    arguments: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
    request_id: int = 10,
) -> tuple[dict[str, Any], dict[str, Any]]:
    response = await instance.handle_message({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments, "_meta": meta},
    })
    assert response is not None
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return result, payload


class WorkflowMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_ids_and_cancellation_notifications_are_bounded(self) -> None:
        for value in (0, server.MAX_SAFE_INTEGER, "call-1", "a:b_c.2"):
            self.assertTrue(server._valid_request_id(value), value)
        for value in (
            True,
            -1,
            server.MAX_SAFE_INTEGER + 1,
            "",
            "x" * 129,
            "call/1",
            "café",
            None,
        ):
            self.assertFalse(server._valid_request_id(value), value)

        valid_cancel = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "call-1", "reason": "Wearer cancelled"},
        }
        self.assertEqual(
            server._cancel_request_key(valid_cancel),
            (str, "call-1"),
        )
        malformed = (
            {**valid_cancel, "id": 1},
            {**valid_cancel, "extra": True},
            {**valid_cancel, "jsonrpc": "1.0"},
            {**valid_cancel, "params": {"requestId": "call/1"}},
            {**valid_cancel, "params": {"requestId": -1}},
            {**valid_cancel, "params": {"requestId": "call-1", "extra": True}},
            {**valid_cancel, "params": {"requestId": "call-1", "reason": "x" * 161}},
        )
        for value in malformed:
            self.assertIsNone(server._cancel_request_key(value), value)

    async def test_manifest_launch_is_dependency_free_and_versioned(self) -> None:
        root = Path(__file__).resolve().parent
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        mcp_config = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], server.SERVER_VERSION)
        launch = mcp_config["mcpServers"]["workflows"]
        self.assertEqual(launch["command"], "python")
        self.assertEqual(
            launch["args"],
            ["-I", "-S", "-B", "${PLUGIN_ROOT}/server.py"],
        )
        self.assertEqual(
            launch["env"],
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "HERMES_G2_WORKFLOW_RELAY": (
                    "${PLUGIN_DATA}/../../run/g2-workflows.sock"
                ),
            },
        )
        source = (root / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("from mcp", source)
        self.assertNotIn("import mcp", source)

    async def test_list_and_call_expose_intent_only_schemas(self) -> None:
        relay = FakeRelay({
            "g2.work_tasks.add": [
                work_receipt("acknowledged"),
                work_receipt("historical_acknowledgement"),
                work_receipt("acknowledged"),
            ],
        })
        instance = await initialized_server(relay)
        listed = await instance.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        self.assertIsNotNone(listed)
        tools = listed["result"]["tools"]
        self.assertEqual([item["name"] for item in tools], [item["name"] for item in server.TOOLS])
        self.assertEqual(len(tools), 13)
        for item in tools:
            self.assertIn("outputSchema", item)
            self.assertEqual(item["inputSchema"].get("type"), "object")
            self.assertEqual(item["outputSchema"].get("type"), "object")
            if "properties" in item["inputSchema"]:
                self.assertIs(item["inputSchema"]["additionalProperties"], False)
                self.assertNotIn("operation_id", item["inputSchema"]["properties"])
                self.assertNotIn("platform", item["inputSchema"]["properties"])
                self.assertNotIn("profile", item["inputSchema"]["properties"])

        train = next(
            item for item in tools
            if item["name"] == "g2_train_departures_present"
        )
        self.assertIn("Liverpool Central is LVC", train["description"])
        self.assertIn(
            "LVC is Liverpool Central",
            train["inputSchema"]["properties"]["destination_crs"]["description"],
        )
        self.assertIn(
            "BLN for Blundellsands & Crosby",
            train["inputSchema"]["properties"]["origin_crs"]["description"],
        )

        blocked_messages = {
            "clock_alert_active": (
                "The glasses display is busy with an active Clock alert."
            ),
            "assistant_presentation_active": (
                "The glasses display is busy with another assistant presentation."
            ),
        }
        for name in ("g2_weather_present", "g2_train_departures_present"):
            item = next(tool for tool in tools if tool["name"] == name)
            variants = item["outputSchema"]["oneOf"]
            blockers = [
                variant for variant in variants
                if variant["properties"].get("state", {}).get("const")
                == "presentation_blocked"
            ]
            self.assertEqual(len(blockers), 2)
            self.assertEqual({
                variant["properties"]["error_code"]["const"]:
                variant["properties"]["error"]["const"]
                for variant in blockers
            }, blocked_messages)
            for variant in blockers:
                self.assertEqual(
                    set(variant["required"]),
                    {"success", "state", "error_code", "error"},
                )
                self.assertIs(variant["additionalProperties"], False)

        result, payload = await call_tool(
            instance,
            "g2_work_task_add",
            {"title": "Email Simon about merger permissions", "lane": "today"},
            meta=session_meta(
                "g2_work_task_add",
                {"title": "Email Simon about merger permissions", "lane": "today"},
                "task-call-1",
            ),
        )
        self.assertIs(result["isError"], False)
        self.assertEqual(result["structuredContent"], payload)
        self.assertIs(payload["success"], True)
        name, arguments, trusted = relay.calls[0]
        self.assertEqual(name, "g2.work_tasks.add")
        self.assertEqual(arguments["title"], "Email Simon about merger permissions")
        self.assertEqual(arguments["lane"], "today")
        self.assertRegex(arguments["operation_id"], r"^task\.[a-f0-9]{32}$")
        self.assertEqual(
            trusted["capability"]["claims"]["tool_call_id"], "task-call-1"
        )
        self.assertEqual(trusted["workflow"], "g2_work_task_add")
        self.assertEqual(trusted["subcall_id"], 1)
        self.assertEqual(trusted["attempt"], 1)

        await call_tool(
            instance,
            "g2_work_task_add",
            {"title": "A changed payload must conflict downstream", "lane": "inbox"},
            meta=session_meta(
                "g2_work_task_add",
                {"title": "A changed payload must conflict downstream", "lane": "inbox"},
                "task-call-1",
            ),
            request_id=11,
        )
        await call_tool(
            instance,
            "g2_work_task_add",
            {"title": "Email Simon about merger permissions", "lane": "today"},
            meta=session_meta(
                "g2_work_task_add",
                {"title": "Email Simon about merger permissions", "lane": "today"},
                "task-call-2",
            ),
            request_id=12,
        )
        self.assertEqual(relay.calls[0][1]["operation_id"], relay.calls[1][1]["operation_id"])
        self.assertNotEqual(relay.calls[1][1]["operation_id"], relay.calls[2][1]["operation_id"])

    async def test_kanban_create_is_exact_parked_and_retry_safe(self) -> None:
        relay = FakeRelay({
            "g2.kanban.task.create": [
                kanban_receipt(),
                {
                    "success": False,
                    "commit_state": "unknown",
                    "operation_id": "ignored-by-retry",
                    "error": "receipt lost",
                },
                kanban_receipt("historical_acknowledgement"),
            ],
        })
        instance = await initialized_server(relay)
        arguments = {
            "title": "Mimecast creation",
            "body": "Waiting for account owner input",
            "board": "Hermes G2",
        }
        result, payload = await call_tool(
            instance,
            "g2_kanban_task_create",
            arguments,
            meta=session_meta(
                "g2_kanban_task_create", arguments, "kanban-call-1"
            ),
        )
        self.assertIs(result["isError"], False)
        self.assertEqual(payload, {
            "success": True,
            "state": "completed",
            "status": "acknowledged",
            "task_id": "t_1234abcd",
            "created_status": "blocked",
            "created_assignee": None,
            "board": "hermes-g2",
        })
        name, native, authorization = relay.calls[0]
        self.assertEqual(name, "g2.kanban.task.create")
        self.assertEqual(native["title"], arguments["title"])
        self.assertEqual(native["body"], arguments["body"])
        self.assertEqual(native["board"], arguments["board"])
        self.assertRegex(native["operation_id"], r"^kanban\.[a-f0-9]{32}$")
        self.assertEqual(authorization["workflow"], "g2_kanban_task_create")

        retry_arguments = {"title": "Retry me", "board": "hermes-g2"}
        retry_result, retry_payload = await call_tool(
            instance,
            "g2_kanban_task_create",
            retry_arguments,
            meta=session_meta(
                "g2_kanban_task_create", retry_arguments, "kanban-call-2"
            ),
            request_id=13,
        )
        self.assertIs(retry_result["isError"], False)
        self.assertEqual(retry_payload["status"], "historical_acknowledgement")
        self.assertEqual(relay.calls[1][1], relay.calls[2][1])
        self.assertEqual(relay.calls[1][2]["subcall_id"], 1)
        self.assertEqual(relay.calls[1][2]["attempt"], 1)
        self.assertEqual(relay.calls[2][2]["subcall_id"], 1)
        self.assertEqual(relay.calls[2][2]["attempt"], 2)

    async def test_kanban_missing_board_returns_typed_bounded_choices(self) -> None:
        arguments = {"title": "Mimecast creation", "board": "Blocker"}
        relay = FakeRelay({
            "g2.kanban.task.create": [{
                "success": False,
                "commit_state": "not_committed",
                "error_code": "board_not_found",
                "error": "No active Hermes Kanban board exactly matches that name",
                "available_boards": [
                    {"slug": "default", "name": "Default"},
                    {"slug": "hermes-g2", "name": "Hermes G2"},
                ],
                "boards_truncated": False,
            }],
        })
        instance = await initialized_server(relay)
        result, payload = await call_tool(
            instance,
            "g2_kanban_task_create",
            arguments,
            meta=session_meta("g2_kanban_task_create", arguments),
        )
        self.assertIs(result["isError"], True)
        self.assertEqual(payload, {
            "success": False,
            "state": "not_committed",
            "error_code": "board_not_found",
            "error": "No active Hermes Kanban board exactly matches that name",
            "available_boards": [
                {"slug": "default", "name": "Default"},
                {"slug": "hermes-g2", "name": "Hermes G2"},
            ],
            "boards_truncated": False,
        })
        self.assertEqual(len(relay.calls), 1)

    async def test_kanban_committed_payload_conflict_is_typed_and_not_retried(
        self,
    ) -> None:
        arguments = {"title": "Changed title", "board": "Hermes G2"}

        def committed_conflict(_name, native, _authorization):
            return {
                "success": False,
                "commit_state": "committed",
                "operation_id": native["operation_id"],
                "error_code": "operation_conflict",
                "error": (
                    "Kanban operation identity is already bound to different "
                    "arguments"
                ),
            }

        relay = FakeRelay({
            "g2.kanban.task.create": [committed_conflict],
        })
        instance = await initialized_server(relay)
        result, payload = await call_tool(
            instance,
            "g2_kanban_task_create",
            arguments,
            meta=session_meta("g2_kanban_task_create", arguments),
        )

        self.assertIs(result["isError"], True)
        self.assertEqual(payload, {
            "success": False,
            "state": "historical_conflict",
            "error_code": "operation_conflict",
            "error": (
                "Kanban operation identity is already bound to different "
                "arguments"
            ),
        })
        self.assertEqual(len(relay.calls), 1)

    async def test_kanban_operation_failures_and_creation_facts_are_exact(
        self,
    ) -> None:
        operation_id = "kanban." + "4" * 32
        messages = server._KANBAN_OPERATION_ERROR_MESSAGES
        cases = (
            ("operation_conflict", "committed", "historical_conflict"),
            ("operation_conflict", "unknown", "outcome_unknown"),
            ("operation_conflict", "not_committed", "not_committed"),
            ("board_generation_changed", "not_committed", "not_committed"),
            ("operation_outcome_unknown", "unknown", "outcome_unknown"),
        )
        for error_code, commit_state, public_state in cases:
            native = {
                "success": False,
                "commit_state": commit_state,
                "operation_id": operation_id,
                "error_code": error_code,
                "error": messages[error_code],
            }
            self.assertEqual(
                server._decode_kanban_task_result(
                    native, operation_id=operation_id
                ),
                {
                    "success": False,
                    "state": public_state,
                    "error_code": error_code,
                    "error": messages[error_code],
                },
            )

        exact_receipt = {
            "success": True,
            "receipt": {
                "status": "historical_acknowledgement",
                "operation_id": operation_id,
                "task_id": "t_1234abcd",
                "created_status": "blocked",
                "created_assignee": None,
                "board": "hermes-g2",
            },
        }
        decoded = server._decode_kanban_task_result(
            exact_receipt, operation_id=operation_id
        )
        self.assertEqual(decoded["created_status"], "blocked")
        self.assertIsNone(decoded["created_assignee"])
        self.assertNotIn("task_status", decoded)

        malformed = copy.deepcopy(exact_receipt)
        malformed["receipt"]["created_assignee"] = "even-g2"
        with self.assertRaises(ValueError):
            server._decode_kanban_task_result(
                malformed, operation_id=operation_id
            )
        wrong_message = {
            "success": False,
            "commit_state": "committed",
            "operation_id": operation_id,
            "error_code": "operation_conflict",
            "error": "untrusted detail",
        }
        with self.assertRaises(ValueError):
            server._decode_kanban_task_result(
                wrong_message, operation_id=operation_id
            )

    async def test_unknown_commit_retries_once_with_the_identical_internal_id(self) -> None:
        relay = FakeRelay({
            "g2.clock.set_timer": [
                {"success": False, "commit_state": "unknown", "error": "receipt lost"},
                timer_receipt("historical_acknowledgement"),
            ],
        })
        instance = await initialized_server(relay)
        result, payload = await call_tool(
            instance,
            "g2_clock_set_timer",
            {"duration_seconds": 600, "label": "Tea"},
            meta=session_meta(
                "g2_clock_set_timer",
                {"duration_seconds": 600, "label": "Tea"},
                "timer-call-9",
            ),
        )
        self.assertIs(result["isError"], False)
        self.assertIs(payload["success"], True)
        self.assertEqual(len(relay.calls), 2)
        self.assertEqual(relay.calls[0][:2], relay.calls[1][:2])
        self.assertEqual(relay.calls[0][2]["capability"], relay.calls[1][2]["capability"])
        self.assertEqual(relay.calls[0][2]["subcall_id"], relay.calls[1][2]["subcall_id"])
        self.assertEqual([call[2]["attempt"] for call in relay.calls], [1, 2])
        operation_id = relay.calls[0][1]["operation_id"]
        self.assertRegex(operation_id, r"^timer\.[a-f0-9]{32}$")
        claims = session_meta(
            "g2_clock_set_timer",
            {"duration_seconds": 600, "label": "Tea"},
            "timer-call-9",
        )[server.CAPABILITY_META_KEY]["claims"]
        self.assertEqual(operation_id, server._stable_operation("timer", claims))

    async def test_alarm_and_reminder_receipts_bind_exact_schedules(self) -> None:
        operation_id = "alarm." + "a" * 32
        alarm = {
            "success": True,
            "receipt": {
                "status": "acknowledged",
                "operation_id": operation_id,
                "item_id": "clk_" + "1" * 32,
                "kind": "alarm",
                "next_fire_at_ms": 1_787_702_999_000,
                "clock_revision": 9,
                "local_time": "07:30",
                "date": "2026-08-26",
                "repeat_days": [],
            },
        }
        decoded = server._decode_clock_result(
            alarm,
            operation_id=operation_id,
            kind="alarm",
            local_time="07:30",
            expected_date="2026-08-26",
            expected_repeat_days=[],
        )
        self.assertEqual(decoded["date"], "2026-08-26")

        mismatched_date = copy.deepcopy(alarm)
        mismatched_date["receipt"]["date"] = "2026-08-27"
        duplicate_days = copy.deepcopy(alarm)
        duplicate_days["receipt"].update({
            "date": None,
            "repeat_days": ["mon", "mon"],
        })
        reordered_days = copy.deepcopy(alarm)
        reordered_days["receipt"].update({
            "date": None,
            "repeat_days": ["fri", "mon"],
        })
        invalid_resolved_date = copy.deepcopy(alarm)
        invalid_resolved_date["receipt"]["date"] = "2026-02-30"
        with self.assertRaises(ValueError):
            server._decode_clock_result(
                mismatched_date,
                operation_id=operation_id,
                kind="alarm",
                local_time="07:30",
                expected_date="2026-08-26",
                expected_repeat_days=[],
            )
        for value in (duplicate_days, reordered_days):
            with self.assertRaises(ValueError):
                server._decode_clock_result(
                    value,
                    operation_id=operation_id,
                    kind="alarm",
                    local_time="07:30",
                    expected_repeat_days=["mon", "fri"],
                )
        with self.assertRaises(ValueError):
            server._decode_clock_result(
                invalid_resolved_date,
                operation_id=operation_id,
                kind="alarm",
                local_time="07:30",
                expected_repeat_days=[],
                allow_resolved_date=True,
            )

        reminder_operation = "rem." + "b" * 32
        reminder = {
            "success": True,
            "status": "scheduled",
            "operation_id": reminder_operation,
            "reminder_id": "c" * 32,
            "due_at": "2026-08-26T07:30:00.000Z",
        }
        self.assertEqual(
            server._decode_reminder_result(
                reminder,
                operation_id=reminder_operation,
            )["due_at"],
            reminder["due_at"],
        )
        impossible_due = copy.deepcopy(reminder)
        impossible_due["due_at"] = "2026-02-30T07:30:00.000Z"
        with self.assertRaises(ValueError):
            server._decode_reminder_result(
                impossible_due,
                operation_id=reminder_operation,
            )

    async def test_device_workflow_uses_one_fixed_relay_and_typed_projection(self) -> None:
        relay = FakeRelay({
            "g2.device.media.control": [{
                "success": True,
                "state": "playing",
                "summary": "Signal · Playing",
            }],
        })
        instance = await initialized_server(relay)
        arguments = {"action": "status"}
        result, payload = await call_tool(
            instance,
            "g2_media_control",
            arguments,
            meta=session_meta("g2_media_control", arguments, "media-call-1"),
        )
        self.assertIs(result["isError"], False)
        self.assertEqual(payload, {
            "success": True,
            "state": "playing",
            "summary": "Signal · Playing",
        })
        self.assertEqual(len(relay.calls), 1)
        self.assertEqual(relay.calls[0][0], "g2.device.media.control")
        self.assertEqual(relay.calls[0][1], arguments)
        self.assertEqual(relay.calls[0][2]["attempt"], 1)

    async def test_mutating_device_transport_failure_is_not_retried(self) -> None:
        relay = FakeRelay({
            "g2.device.media.control": [server.RelayError("response lost")],
        })
        instance = await initialized_server(relay)
        arguments = {"action": "next"}
        result, payload = await call_tool(
            instance,
            "g2_media_control",
            arguments,
            meta=session_meta("g2_media_control", arguments, "media-call-2"),
        )
        self.assertIs(result["isError"], True)
        self.assertEqual(payload["error_code"], "outcome_unknown")
        self.assertEqual(len(relay.calls), 1)

    async def test_trusted_session_metadata_is_exact_and_model_arguments_cannot_claim_it(self) -> None:
        relay = FakeRelay()
        instance = await initialized_server(relay)
        args = {"title": "Final result"}
        valid = session_meta("g2_work_task_add", args)
        invalid: list[dict[str, Any] | None] = [None]
        for key, replacement in (
            ("tool_call_id", None),
            ("platform", "api_server"),
            ("profile", "Even-G2"),
            ("message_id", "not-a-g2-turn"),
            ("version", True),
        ):
            candidate = copy.deepcopy(valid)
            if replacement is None:
                del candidate[server.CAPABILITY_META_KEY]["claims"][key]
            else:
                candidate[server.CAPABILITY_META_KEY]["claims"][key] = replacement
            invalid.append(candidate)
        extra = copy.deepcopy(valid)
        extra[server.CAPABILITY_META_KEY]["claims"]["model_claim"] = "g2"
        invalid.append(extra)

        for index, meta in enumerate(invalid, start=20):
            result, payload = await call_tool(
                instance,
                "g2_work_task_add",
                args,
                meta=meta,
                request_id=index,
            )
            self.assertIs(result["isError"], True)
            self.assertIn("trusted", payload["error"])
        self.assertEqual(relay.calls, [])

        result, _payload = await call_tool(
            instance,
            "g2_work_task_add",
            {"title": "Final result", "operation_id": "model-controlled"},
            meta=valid,
            request_id=40,
        )
        self.assertIs(result["isError"], True)
        self.assertEqual(relay.calls, [])

    async def test_weather_call_builds_only_a_typed_attributed_present_shape(self) -> None:
        weather = {
            "success": True,
            "trust": "typed_open_meteo_ukmo_data",
            "dashboard_key": "weather-0123456789abcdef0123456789abcdef",
            "title": "Liverpool · Tomorrow",
            "result": {
                "location_label": "Liverpool",
                "date": "2026-08-26",
                "weather_code": 61,
                "condition": "rain",
                "temperature_min_c": 9.7,
                "temperature_max_c": 17.2,
                "precipitation_probability_max_pct": 70,
                "precipitation_amount_mm": 3.4,
                "wind_speed_max_kmh": 29.6,
                "source": "Open-Meteo · UK Met Office data",
                "observed_at_ms": 1_787_702_399_000,
            },
        }
        relay = FakeRelay({
            "g2.weather.read_forecast": [weather],
            "g2.context.present": [present_receipt()],
        })
        instance = await initialized_server(relay)
        result, payload = await call_tool(
            instance,
            "g2_weather_present",
            {"location": "Liverpool", "day_offset": 1},
            meta=session_meta(
                "g2_weather_present",
                {"location": "Liverpool", "day_offset": 1},
                "weather-call-1",
            ),
        )
        self.assertIs(result["isError"], False)
        self.assertEqual(payload, {
            "success": True,
            "dashboard_key": weather["dashboard_key"],
            "title": weather["title"],
            "summary": "Rain · 9.7–17.2°C",
        })
        self.assertEqual([call[0] for call in relay.calls], [
            "g2.weather.read_forecast", "g2.context.present",
        ])
        self.assertNotIn("operation_id", relay.calls[0][1])
        present = relay.calls[1][1]
        self.assertRegex(present["operation_id"], r"^weather\.[a-f0-9]{32}$")
        spec = present["spec"]
        self.assertEqual(spec["presentation_mode"], "deck")
        self.assertEqual(spec["privacy"], "private")
        self.assertEqual(spec["state"], "ready")
        self.assertEqual(spec["sources"][0]["attribution_id"], "open_meteo_ukmo")
        self.assertEqual(spec["sources"][0]["observed_at_ms"], 1_787_702_399_000)
        self.assertEqual(spec["sections"][0]["type"], "status_grid")
        self.assertEqual(spec["sections"][0]["rows"][2]["value"], "70% · 3.4 mm")

    async def test_train_call_builds_only_a_typed_departures_present_shape(self) -> None:
        train = {
            "success": True,
            "trust": "typed_national_rail_data",
            "result": {
                "source": "National Rail",
                "origin_crs": "BLN",
                "destination_crs": "LVC",
                "data_kind": "live",
                "observed_at_ms": 1_787_702_399_000,
                "departures": [{
                    "scheduled_departure_ms": 1_787_702_400_000,
                    "scheduled_arrival_ms": 1_787_703_000_000,
                    "expected_departure_ms": 1_787_702_460_000,
                    "expected_arrival_ms": 1_787_703_060_000,
                    "platform": "2",
                    "status": "delayed",
                }],
            },
        }
        relay = FakeRelay({
            "g2.transit.read_departures": [train],
            "g2.context.present": [present_receipt()],
        })
        instance = await initialized_server(relay)
        result, payload = await call_tool(
            instance,
            "g2_train_departures_present",
            {"origin_crs": "BLN", "destination_crs": "LVC"},
            meta=session_meta(
                "g2_train_departures_present",
                {"origin_crs": "BLN", "destination_crs": "LVC"},
                "train-call-1",
            ),
        )
        self.assertIs(result["isError"], False)
        self.assertIs(payload["success"], True)
        self.assertRegex(payload["dashboard_key"], r"^rail-[a-f0-9]{24}$")
        self.assertEqual(payload["title"], "BLN to LVC")
        present = relay.calls[1][1]
        self.assertRegex(present["operation_id"], r"^train\.[a-f0-9]{32}$")
        spec = present["spec"]
        self.assertEqual(spec["state"], "ready")
        self.assertEqual(spec["sections"][0]["type"], "departures")
        self.assertEqual(spec["sections"][0]["rows"], [{
            "id": "service-1",
            "destination": "LVC",
            "scheduled_departure_ms": 1_787_702_400_000,
            "expected_departure_ms": 1_787_702_460_000,
            "platform": "2",
            "status": "delayed",
        }])
        self.assertEqual(spec["sources"][0]["observed_at_ms"], 1_787_702_399_000)

    async def test_weather_and_train_preserve_exact_display_busy_rejections(self) -> None:
        weather = {
            "success": True,
            "trust": "typed_open_meteo_ukmo_data",
            "dashboard_key": "weather-0123456789abcdef0123456789abcdef",
            "title": "Liverpool · Tomorrow",
            "result": {
                "location_label": "Liverpool",
                "date": "2026-08-26",
                "weather_code": 61,
                "condition": "rain",
                "temperature_min_c": 9.7,
                "temperature_max_c": 17.2,
                "precipitation_probability_max_pct": 70,
                "precipitation_amount_mm": 3.4,
                "wind_speed_max_kmh": 29.6,
                "source": "Open-Meteo · UK Met Office data",
                "observed_at_ms": 1_787_702_399_000,
            },
        }
        train = {
            "success": True,
            "trust": "typed_national_rail_data",
            "result": {
                "source": "National Rail",
                "origin_crs": "BLN",
                "destination_crs": "LVC",
                "data_kind": "live",
                "observed_at_ms": 1_787_702_399_000,
                "departures": [{
                    "scheduled_departure_ms": 1_787_702_400_000,
                    "scheduled_arrival_ms": 1_787_703_000_000,
                    "status": "on_time",
                }],
            },
        }
        relay = FakeRelay({
            "g2.weather.read_forecast": [weather],
            "g2.transit.read_departures": [train],
            "g2.context.present": [
                present_blocked("clock_alert_active"),
                present_blocked("assistant_presentation_active"),
            ],
        })
        instance = await initialized_server(relay)

        weather_result, weather_payload = await call_tool(
            instance,
            "g2_weather_present",
            {"location": "Liverpool", "day_offset": 1},
            meta=session_meta(
                "g2_weather_present",
                {"location": "Liverpool", "day_offset": 1},
                "weather-display-busy",
            ),
        )
        train_result, train_payload = await call_tool(
            instance,
            "g2_train_departures_present",
            {"origin_crs": "BLN", "destination_crs": "LVC"},
            meta=session_meta(
                "g2_train_departures_present",
                {"origin_crs": "BLN", "destination_crs": "LVC"},
                "train-display-busy",
            ),
            request_id=11,
        )

        self.assertIs(weather_result["isError"], True)
        self.assertEqual(weather_payload, {
            "success": False,
            "state": "presentation_blocked",
            "error_code": "clock_alert_active",
            "error": "The glasses display is busy with an active Clock alert.",
        })
        self.assertIs(train_result["isError"], True)
        self.assertEqual(train_payload, {
            "success": False,
            "state": "presentation_blocked",
            "error_code": "assistant_presentation_active",
            "error": "The glasses display is busy with another assistant presentation.",
        })

    async def test_context_present_receipt_is_exact_and_bound_to_the_requested_frame(self) -> None:
        operation_id = "weather." + "a" * 32
        dashboard_key = "weather-" + "b" * 32
        good = {
            "success": True,
            "receipt": {
                "status": "acknowledged",
                "operation_id": operation_id,
                "dashboard_key": dashboard_key,
                "dashboard_id": "ctx_" + "c" * 32,
                "presentation_generation": 1,
                "refresh_generation": 1,
                "revision": 1,
                "frame_id": 17,
            },
        }
        receipt = server._decode_present_result(
            good,
            operation_id=operation_id,
            dashboard_key=dashboard_key,
        )
        self.assertEqual(receipt["frame_id"], 17)

        blocked_messages = {
            "clock_alert_active": (
                "The glasses display is busy with an active Clock alert."
            ),
            "assistant_presentation_active": (
                "The glasses display is busy with another assistant presentation."
            ),
        }
        for error_code, error in blocked_messages.items():
            blocked = {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": operation_id,
                "error_code": error_code,
                "error": error,
            }
            self.assertEqual(
                server._decode_present_result(
                    blocked,
                    operation_id=operation_id,
                    dashboard_key=dashboard_key,
                ),
                {
                    "success": False,
                    "state": "presentation_blocked",
                    "error_code": error_code,
                    "error": error,
                },
            )

        historical = copy.deepcopy(good)
        historical["receipt"]["status"] = "historical_acknowledgement"
        self.assertEqual(
            server._decode_present_result(
                historical,
                operation_id=operation_id,
                dashboard_key=dashboard_key,
            )["status"],
            "historical_acknowledgement",
        )

        malformed: dict[str, Any] = {
            "legacy generic non-error": {
                "success": True,
                "result": {"isError": False},
            },
            "mismatched operation": copy.deepcopy(good),
            "mismatched dashboard key": copy.deepcopy(good),
            "wrong revision": copy.deepcopy(good),
            "wrong presentation generation": copy.deepcopy(good),
            "wrong refresh generation": copy.deepcopy(good),
            "zero frame": copy.deepcopy(good),
            "boolean frame": copy.deepcopy(good),
            "unrecognised status": copy.deepcopy(good),
            "short dashboard id": copy.deepcopy(good),
            "extra receipt field": copy.deepcopy(good),
            "missing receipt field": copy.deepcopy(good),
            "unknown rejection code": {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": operation_id,
                "error_code": "feed_unavailable",
                "error": "private provider detail",
            },
            "non-string rejection code": {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": operation_id,
                "error_code": ["clock_alert_active"],
                "error": "private provider detail",
            },
            "rejection message mismatch": {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": operation_id,
                "error_code": "clock_alert_active",
                "error": "private provider detail",
            },
            "rejection operation mismatch": {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": "weather." + "d" * 32,
                "error_code": "clock_alert_active",
                "error": blocked_messages["clock_alert_active"],
            },
            "extra rejection field": {
                "success": False,
                "commit_state": "not_committed",
                "operation_id": operation_id,
                "error_code": "clock_alert_active",
                "error": blocked_messages["clock_alert_active"],
                "detail": "private provider detail",
            },
        }
        malformed["mismatched operation"]["receipt"]["operation_id"] = "weather." + "d" * 32
        malformed["mismatched dashboard key"]["receipt"]["dashboard_key"] = "weather-error"
        malformed["wrong revision"]["receipt"]["revision"] = 2
        malformed["wrong presentation generation"]["receipt"]["presentation_generation"] = 2
        malformed["wrong refresh generation"]["receipt"]["refresh_generation"] = 2
        malformed["zero frame"]["receipt"]["frame_id"] = 0
        malformed["boolean frame"]["receipt"]["frame_id"] = True
        malformed["unrecognised status"]["receipt"]["status"] = "queued"
        malformed["short dashboard id"]["receipt"]["dashboard_id"] = "short"
        malformed["extra receipt field"]["receipt"]["isError"] = False
        del malformed["missing receipt field"]["receipt"]["frame_id"]
        for label, value in malformed.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    server._decode_present_result(
                        value,
                        operation_id=operation_id,
                        dashboard_key=dashboard_key,
                    )

    async def test_malformed_typed_reads_never_reach_present(self) -> None:
        bad_weather = {
            "success": True,
            "trust": "typed_open_meteo_ukmo_data",
            "dashboard_key": "weather-0123456789abcdef0123456789abcdef",
            "title": "Liverpool",
            "result": {"condition": "rain"},
        }
        bad_train = {
            "success": True,
            "trust": "typed_national_rail_data",
            "result": {
                "source": "National Rail",
                "origin_crs": "OTHER",
                "destination_crs": "LVC",
                "data_kind": "live",
                "observed_at_ms": 1,
                "departures": [],
            },
        }
        relay = FakeRelay({
            "g2.weather.read_forecast": [bad_weather],
            "g2.transit.read_departures": [bad_train],
        })
        instance = await initialized_server(relay)
        _weather_result, weather_payload = await call_tool(
            instance, "g2_weather_present", {"location": "Liverpool"},
            meta=session_meta(
                "g2_weather_present", {"location": "Liverpool"}, "bad-weather"
            ), request_id=50,
        )
        _train_result, train_payload = await call_tool(
            instance, "g2_train_departures_present",
            {"origin_crs": "BLN", "destination_crs": "LVC"},
            meta=session_meta(
                "g2_train_departures_present",
                {"origin_crs": "BLN", "destination_crs": "LVC"},
                "bad-train",
            ), request_id=51,
        )
        self.assertIs(weather_payload["success"], False)
        self.assertIs(train_payload["success"], False)
        self.assertNotIn("g2.context.present", [call[0] for call in relay.calls])


if __name__ == "__main__":
    unittest.main()
