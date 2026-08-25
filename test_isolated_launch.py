from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _readline(process: subprocess.Popen[str], timeout: float = 2.0) -> dict[str, Any]:
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    if not readable:
        raise AssertionError("isolated MCP server did not respond")
    raw = process.stdout.readline()
    if not raw:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"isolated MCP server exited early: {stderr}")
    return json.loads(raw)


class IsolatedLaunchTests(unittest.TestCase):
    def test_isolated_launch_imports_locals_without_creating_executable_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            package = temp_root / "package"
            package.mkdir()
            for name in (
                "server.py",
                "relay_client.py",
                "device_workflows.py",
                "plugin.json",
                "mcp.json",
            ):
                shutil.copy2(ROOT / name, package / name)

            # These ambient hooks would execute under an ordinary Python
            # startup. -I -S ensures they cannot run outside the granted
            # package digest, while -B and the env guard prevent pyc creation.
            hostile_home = temp_root / "ambient"
            hostile_home.mkdir()
            (hostile_home / "sitecustomize.py").write_text(
                "raise RuntimeError('ambient startup hook executed')\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(hostile_home),
                "PYTHONPATH": str(hostile_home),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", str(package / "server.py")],
                cwd=package,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(_canonical({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "isolation-test", "version": "1"},
                    },
                }) + "\n")
                process.stdin.flush()
                initialized = _readline(process)
                self.assertEqual(initialized["result"]["serverInfo"]["name"], "hermes-g2-workflows")

                process.stdin.write(_canonical({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }) + "\n")
                arguments = {"title": "Isolation check"}
                now = int(time.time())
                capability = {
                    "claims": {
                        "version": 1,
                        "audience": "com.hermes.mcp/portable/hermes-g2-workflows/workflows",
                        "binding": "hermes-g2-workflows:workflows",
                        "package_digest": "sha256:" + "a" * 64,
                        "platform": "g2",
                        "profile": "even-g2",
                        "chat_id": "glasses",
                        "session_id": "agent:main:g2:dm:glasses",
                        "message_id": "g2-turn-isolation-1",
                        "tool_call_id": "tool-isolation-1",
                        "workflow": "g2_work_task_add",
                        "arguments_sha256": hashlib.sha256(
                            _canonical(arguments).encode("utf-8")
                        ).hexdigest(),
                        "issued_at": now,
                        "expires_at": now + 60,
                        "nonce": "b" * 32,
                    },
                    # The standalone process treats this as opaque. The native
                    # gateway relay, absent in this test, owns HMAC verification.
                    "signature": "c" * 64,
                }
                process.stdin.write(_canonical({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "g2_work_task_add",
                        "arguments": arguments,
                        "_meta": {"com.hermes/capability": capability},
                    },
                }) + "\n")
                process.stdin.flush()
                called = _readline(process)
                self.assertTrue(called["result"]["isError"])
                self.assertEqual(
                    called["result"]["structuredContent"]["state"],
                    "outcome_unknown",
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

            self.assertFalse(any(package.rglob("__pycache__")))
            self.assertFalse(any(package.rglob("*.pyc")))


if __name__ == "__main__":
    unittest.main()
