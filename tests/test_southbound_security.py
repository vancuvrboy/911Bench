"""Tests for southbound transport hardening configuration."""

from __future__ import annotations

import unittest

from gov_server.southbound import SimulationSouthboundClient, SouthboundHTTPError


class SouthboundSecurityConfigTest(unittest.TestCase):
    def test_mtls_requires_https(self) -> None:
        with self.assertRaises(SouthboundHTTPError) as ctx:
            SimulationSouthboundClient(base_url="http://127.0.0.1:8300", require_mtls=True)
        self.assertEqual(str(ctx.exception), "southbound_mtls_requires_https")

    def test_client_key_without_cert_rejected(self) -> None:
        with self.assertRaises(SouthboundHTTPError) as ctx:
            SimulationSouthboundClient(
                base_url="https://sim.example.local",
                require_mtls=True,
                client_key_file="client.key",
            )
        self.assertEqual(str(ctx.exception), "southbound_client_key_without_cert")

    def test_https_client_without_mtls_allowed(self) -> None:
        client = SimulationSouthboundClient(base_url="https://sim.example.local", require_mtls=False)
        self.assertEqual(client.base_url, "https://sim.example.local")


if __name__ == "__main__":
    unittest.main()
