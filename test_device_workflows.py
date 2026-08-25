from __future__ import annotations

import json
import unittest

import device_workflows as workflows
import server


EXPECTED_SURFACE = (
    "g2_apps_manage",
    "g2_media_control",
    "g2_navigation",
    "g2_notifications",
    "g2_health_summary",
    "g2_calendar_agenda",
)

RAW_PHONE_NAMES = (
    "apps.launch",
    "apps.list_windows",
    "apps.focus_window",
    "apps.close_window",
    "apps.list_folders",
    "apps.move_to_folder",
    "apps.remove_from_folder",
    "apps.disband_folder",
    "media.now_playing",
    "media.play_pause",
    "media.next",
    "nav.start_navigation",
    "nav.stop_navigation",
    "nav.route_status",
    "notifications.list",
    "notifications.dismiss",
    "health.get_ring_data",
    "calendar.list_events",
    "glasses_list_tools",
    "glasses_call",
)

FINAL_MODEL_SURFACE = (
    "g2_work_task_add",
    "g2_clock_set_timer",
    "g2_clock_set_alarm",
    "g2_reminder_create",
    "g2_weather_present",
    "g2_train_departures_present",
    *EXPECTED_SURFACE,
)


class DeviceWorkflowTests(unittest.TestCase):
    def test_full_standalone_inventory_is_golden_and_proxy_free(self) -> None:
        self.assertEqual(tuple(item["name"] for item in server.TOOLS), FINAL_MODEL_SURFACE)
        encoded = json.dumps(server.TOOLS, sort_keys=True)
        for raw_name in RAW_PHONE_NAMES:
            self.assertNotIn(raw_name, encoded)
        self.assertNotIn("g2_notify_completed_result", encoded)
        self.assertNotIn("g2_device_call", encoded)

    def test_golden_model_surface_is_static_and_contains_no_raw_phone_route(self) -> None:
        self.assertEqual(tuple(item["name"] for item in workflows.DEVICE_TOOLS), EXPECTED_SURFACE)
        self.assertEqual(workflows.DEVICE_TOOL_NAMES, frozenset(EXPECTED_SURFACE))
        encoded = json.dumps(workflows.DEVICE_TOOLS, sort_keys=True)
        for raw_name in RAW_PHONE_NAMES:
            self.assertNotIn(raw_name, encoded)
        self.assertNotIn("g2_notify_completed_result", encoded)
        self.assertNotIn("operation_id", encoded)
        for tool in workflows.DEVICE_TOOLS:
            self.assertEqual(set(tool), {"name", "description", "inputSchema", "outputSchema"})
            self.assertEqual(tool["inputSchema"].get("type"), "object")
            self.assertEqual(tool["outputSchema"].get("type"), "object")
            self.assertIn("oneOf", tool["outputSchema"])
            for output in tool["outputSchema"]["oneOf"]:
                self.assertFalse(output.get("additionalProperties", True))

    def test_every_action_variant_is_an_exact_bounded_object(self) -> None:
        for tool in workflows.DEVICE_TOOLS[:4]:
            variants = tool["inputSchema"]["oneOf"]
            self.assertGreaterEqual(len(variants), 2)
            actions = set()
            for variant in variants:
                self.assertEqual(variant["type"], "object")
                self.assertIs(variant["additionalProperties"], False)
                self.assertIn("action", variant["required"])
                action = variant["properties"]["action"]["const"]
                self.assertNotIn(action, actions)
                actions.add(action)
        for tool in workflows.DEVICE_TOOLS[4:]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIs(tool["inputSchema"]["additionalProperties"], False)

    def test_normalization_routes_only_to_six_fixed_internal_workflows(self) -> None:
        cases = {
            "g2_apps_manage": {"action": "launch", "app_id": "music"},
            "g2_media_control": {"action": "status"},
            "g2_navigation": {"action": "start", "destination": "Liverpool Central"},
            "g2_notifications": {"action": "list"},
            "g2_health_summary": {},
            "g2_calendar_agenda": {},
        }
        expected = {
            "g2.device.apps.manage",
            "g2.device.media.control",
            "g2.device.navigation",
            "g2.device.notifications",
            "g2.device.health.summary",
            "g2.device.calendar.agenda",
        }
        routed = {
            workflows.normalize_device_invocation(name, arguments).workflow
            for name, arguments in cases.items()
        }
        self.assertEqual(routed, expected)

        notification = workflows.normalize_device_invocation(
            "g2_notifications", {"action": "list"}
        )
        self.assertEqual(notification.arguments, {"action": "list", "max": 10})
        self.assertFalse(notification.mutating)
        launch = workflows.normalize_device_invocation(
            "g2_apps_manage", {"action": "launch", "app_id": "music"}
        )
        self.assertTrue(launch.mutating)

    def test_raw_or_legacy_names_and_cross_action_fields_reject(self) -> None:
        for name in (*RAW_PHONE_NAMES, "g2_notify_completed_result", "g2_device_call"):
            with self.assertRaises(workflows.DeviceInputError):
                workflows.normalize_device_invocation(name, {})
        invalid = (
            ("g2_apps_manage", {"action": "list_windows", "app_id": "music"}),
            ("g2_apps_manage", {"action": "launch", "app_id": "terminal"}),
            ("g2_navigation", {"action": "status", "destination": "ignored"}),
            ("g2_notifications", {"action": "dismiss", "key": "x", "max": 2}),
            ("g2_health_summary", {"days": True}),
            ("g2_calendar_agenda", {"within_hours": 721}),
        )
        for name, arguments in invalid:
            with self.assertRaises(workflows.DeviceInputError):
                workflows.normalize_device_invocation(name, arguments)

    def test_typed_result_boundary_rejects_raw_mcp_or_unbounded_shapes(self) -> None:
        with self.assertRaises(workflows.DeviceOutputError):
            workflows.validate_device_result(
                "g2_media_control",
                {"success": True, "state": "completed", "content": []},
            )
        with self.assertRaises(workflows.DeviceOutputError):
            workflows.validate_device_result(
                "g2_calendar_agenda",
                {"success": True, "state": "available", "events": ["x" * 30_000]},
            )
        accepted = workflows.validate_device_result(
            "g2_notifications",
            {
                "success": True,
                "state": "available",
                "notifications": [{"key": "k", "app": "Doorbell", "summary": "Motion"}],
            },
        )
        self.assertEqual(accepted["notifications"][0]["app"], "Doorbell")

    def test_exact_receipt_validation_rejects_cross_domain_or_extra_fields(self) -> None:
        invalid = (
            ("g2_apps_manage", {"success": True, "state": "playing", "summary": "Track"}),
            ("g2_media_control", {"success": True, "state": "active", "summary": "Route"}),
            ("g2_navigation", {"success": True, "state": "completed"}),
            ("g2_notifications", {
                "success": True,
                "state": "available",
                "notifications": [{"key": "same", "app": "A", "summary": "one"},
                                  {"key": "same", "app": "B", "summary": "two"}],
            }),
            ("g2_health_summary", {
                "success": True,
                "state": "empty",
                "range": {"start_date": "2026-08-25", "end_date": "2026-08-25"},
                "days_with_data": 1,
                "latest": None,
            }),
            ("g2_calendar_agenda", {
                "success": True,
                "state": "empty",
                "events": [],
                "raw": "not allowed",
            }),
        )
        for name, value in invalid:
            with self.assertRaises(workflows.DeviceOutputError):
                workflows.validate_device_result(name, value)


class FullSurfaceRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_old_names_reject_before_any_relay_or_capability_use(self) -> None:
        class RejectRelay:
            async def __call__(self, *_args, **_kwargs):
                raise AssertionError("unknown model tool reached the native relay")

        instance = server.WorkflowMcpServer(server.WorkflowService(RejectRelay()))
        initialized = await instance.handle_message({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "golden-test", "version": "1"},
            },
        })
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        await instance.handle_message({
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })
        for index, name in enumerate((*RAW_PHONE_NAMES, "g2_notify_completed_result"), 10):
            response = await instance.handle_message({
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            })
            self.assertEqual(response["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
