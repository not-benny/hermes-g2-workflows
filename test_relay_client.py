from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import relay_client


class RelayEndpointTests(unittest.TestCase):
    def test_exact_profile_scoped_endpoint_is_resolved(self) -> None:
        raw = "/profiles/even-g2/plugin-data/workflows/../../run/g2-workflows.sock"
        with patch.dict(os.environ, {"HERMES_G2_WORKFLOW_RELAY": raw}, clear=True):
            self.assertEqual(
                relay_client._socket_path(),
                Path("/profiles/even-g2/run/g2-workflows.sock"),
            )

    def test_missing_or_malformed_endpoint_fails_closed(self) -> None:
        invalid = (None, "relative/g2-workflows.sock", "/tmp/other.sock")
        for value in invalid:
            environment = {} if value is None else {"HERMES_G2_WORKFLOW_RELAY": value}
            with self.subTest(value=value), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(relay_client.RelayError):
                    relay_client._socket_path()


if __name__ == "__main__":
    unittest.main()
