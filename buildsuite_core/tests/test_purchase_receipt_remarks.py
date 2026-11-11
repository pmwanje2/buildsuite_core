"""BuildSuite must leave native Purchase Receipt remarks and audit history intact.

These offline tests dispatch the app's real on_update registration. Frappe saves
that returned document in its response and uses it for native Version tracking;
BuildSuite must not replace the operator's text or append authorship there.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def _on_update(doc):
	tree = ast.parse((ROOT / "buildsuite_core" / "hooks.py").read_text())
	assignment = next(
		node
		for node in tree.body
		if isinstance(node, ast.Assign)
		and any(isinstance(target, ast.Name) and target.id == "doc_events" for target in node.targets)
	)
	hooks = ast.literal_eval(assignment.value)
	handlers = hooks.get("Purchase Receipt", {}).get("on_update", [])
	if isinstance(handlers, str):
		handlers = [handlers]
	frappe = types.ModuleType("frappe")
	frappe.session = types.SimpleNamespace(user="operator@example.test")
	frappe.db = types.SimpleNamespace(get_value=Mock(return_value="Receiving Operator"))
	for handler in handlers:
		module_name, function_name = handler.rsplit(".", 1)
		path = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
		spec = importlib.util.spec_from_file_location("purchase_receipt_hook_under_test", path)
		module = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {"frappe": frappe}):
			spec.loader.exec_module(module)
			getattr(module, function_name)(doc, "on_update")


def _receipt(remarks, old_remarks):
	return types.SimpleNamespace(
		remarks=remarks,
		get_doc_before_save=lambda: types.SimpleNamespace(remarks=old_remarks),
		supplier="Supplier A",
		items=[types.SimpleNamespace(item_name="Cement")],
		total_qty=2,
		net_total=100,
		project="Project A",
		set_warehouse="Stores A",
		rejected_warehouse=None,
	)


class TestPurchaseReceiptRemarks(unittest.TestCase):
	def test_unchanged_operator_remarks_survive_unrelated_save(self):
		doc = _receipt(
			"Two bags torn; accepted after inspection.", "Two bags torn; accepted after inspection."
		)
		_on_update(doc)
		self.assertEqual(doc.remarks, "Two bags torn; accepted after inspection.")

	def test_edited_remarks_remain_exactly_as_entered(self):
		doc = _receipt("One bag rejected.\nKeep the rest dry.", "Two bags torn.")
		_on_update(doc)
		self.assertEqual(doc.remarks, "One bag rejected.\nKeep the rest dry.")

	def test_operator_can_clear_remarks(self):
		for empty in ("", None):
			with self.subTest(empty=empty):
				doc = _receipt(empty, "Old inspection note.")
				_on_update(doc)
				self.assertEqual(doc.remarks, empty)

	def test_blank_remarks_are_not_replaced_by_a_receipt_summary(self):
		doc = _receipt("", "")
		_on_update(doc)
		self.assertEqual(doc.remarks, "")

	def test_existing_authorship_text_is_not_rewritten(self):
		note = "Legacy note - Updated by Original Receiver"
		doc = _receipt(note, note)
		_on_update(doc)
		self.assertEqual(doc.remarks, note)


if __name__ == "__main__":
	unittest.main()
