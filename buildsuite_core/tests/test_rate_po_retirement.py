# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""The old PO endpoint must refuse before parsing, reading or changing any rate."""

import unittest
from unittest.mock import Mock, patch

from buildsuite_core.tests._boq_stub import ensure_frappe_stub

ensure_frappe_stub()

from buildsuite_core.api import rate_master


class TestRatePORetirement(unittest.TestCase):
	def test_all_roles_and_payloads_receive_plain_refusal_with_stable_code(self):
		for roles in (["System Manager"], ["Procurement Officer"], ["Foreman / Supervisor"]):
			for updates in ([], [{"rate_master": "RM-1", "new_rate": 150}], "not-json"):
				with self.subTest(roles=roles, updates=updates):
					boundary = Mock()
					boundary.get_roles.return_value = roles
					boundary.get_doc.return_value.current_rate = 100
					refusal = ValueError("retired")
					boundary.throw.side_effect = refusal
					with patch.object(rate_master, "frappe", boundary):
						with self.assertRaises(ValueError) as caught:
							rate_master.update_rates_from_po("PO-1", updates, supplier="Supplier-1")
					self.assertIs(caught.exception, refusal)
					boundary.throw.assert_called_once_with(
						"Purchase orders no longer change estimating rates. Use Price changes to review.",
						title="RATE_PO_PATH_RETIRED",
					)
					boundary.get_roles.assert_not_called()
					boundary.get_doc.assert_not_called()
					self.assertEqual(boundary.db.mock_calls, [])
