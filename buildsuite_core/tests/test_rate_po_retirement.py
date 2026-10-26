# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""The old PO endpoint must refuse before parsing, reading or changing any rate."""

import unittest
from functools import wraps
from inspect import signature, unwrap
from types import UnionType
from unittest.mock import Mock, patch

from buildsuite_core.tests._boq_stub import ensure_frappe_stub

ensure_frappe_stub()

from buildsuite_core.api import rate_master

try:
	from frappe.utils.typing_validations import validate_argument_types
except ModuleNotFoundError:
	def validate_argument_types(function, apply_condition=None):
		"""Bench-less seam for Frappe 62dfbccf's whitelist string/union validation.

		Like the pinned validator: validate supplied annotated arguments before the
		body; accept unannotated arguments and declared default types. No parsing.
		Only str/str|None are needed by this endpoint; other annotations fail closed.
		"""
		@wraps(function)
		def wrapper(*args, **kwargs):
			if apply_condition is None or apply_condition():
				parameters = signature(function)
				provided = parameters.bind(*args, **kwargs).arguments
				for name, annotation in function.__annotations__.items():
					if name not in provided or name == "return":
						continue
					types = annotation.__args__ if isinstance(annotation, UnionType) else (annotation,)
					if parameters.parameters[name].default is not parameters.empty:
						types += (type(parameters.parameters[name].default),)
					if not all(kind in (str, type(None)) for kind in types):
						raise AssertionError(f"Unsupported validator seam type: {annotation}")
					if not isinstance(provided[name], types):
						raise TypeError(f"Argument {name} should be of type {annotation}")
			return function(*args, **kwargs)

		return wrapper


class TestRatePORetirement(unittest.TestCase):
	def test_all_roles_and_payloads_receive_plain_refusal_with_stable_code(self):
		# Force the request/test-context path even when imported outside a request.
		endpoint = validate_argument_types(
			unwrap(rate_master.update_rates_from_po), apply_condition=lambda: True,
		)
		for roles in (["System Manager"], ["Procurement Officer"], ["Foreman / Supervisor"]):
			for updates in ([], [{"rate_master": "RM-1", "new_rate": 150}], "not-json", "", None):
				with self.subTest(roles=roles, updates=updates):
					boundary = Mock()
					boundary.get_roles.return_value = roles
					boundary.get_doc.return_value.current_rate = 100
					refusal = ValueError("retired")
					boundary.throw.side_effect = refusal
					with patch.object(rate_master, "frappe", boundary):
						with self.assertRaises((ValueError, TypeError)) as caught:
							endpoint("PO-1", updates, supplier="Supplier-1")
					self.assertIs(caught.exception, refusal)
					boundary.throw.assert_called_once_with(
						"Purchase orders no longer change estimating rates. Use Price changes to review.",
						title="RATE_PO_PATH_RETIRED",
					)
					boundary.get_roles.assert_not_called()
					boundary.get_doc.assert_not_called()
					self.assertEqual(boundary.db.mock_calls, [])

	def test_empty_and_untyped_payloads_reach_retirement_before_whitelist_validation(self):
		endpoint = validate_argument_types(
			unwrap(rate_master.update_rates_from_po), apply_condition=lambda: True,
		)
		for payload in ({}, {"updates": []}, {"purchase_order": [], "supplier": {}}, {"legacy": True}):
			with self.subTest(payload=payload):
				boundary = Mock()
				refusal = ValueError("retired")
				boundary.throw.side_effect = refusal
				with patch.object(rate_master, "frappe", boundary):
					with self.assertRaises((ValueError, TypeError)) as caught:
						endpoint(**payload)
				self.assertIs(caught.exception, refusal)
				boundary.throw.assert_called_once_with(
					"Purchase orders no longer change estimating rates. Use Price changes to review.",
					title="RATE_PO_PATH_RETIRED",
				)
				boundary.get_doc.assert_not_called()
				self.assertEqual(boundary.db.mock_calls, [])
