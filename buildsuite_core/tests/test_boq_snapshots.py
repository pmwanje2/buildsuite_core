# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""Real pricing validators; only documents and decimal(21,9) storage are doubled."""

import sys
import unittest
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from buildsuite_core.tests._boq_stub import AssignmentFlags, ensure_frappe_stub

ensure_frappe_stub()

from buildsuite_core.api import boq as api

try:
	from frappe.model.document import Document
except ModuleNotFoundError:
	document_module = ModuleType("frappe.model.document")
	document_module.Document = object
	with patch.dict(sys.modules, {"frappe.model.document": document_module}):
		from buildsuite_core.buildsuite_core.doctype.boq_item import boq_item
		from buildsuite_core.buildsuite_core.doctype.boq_sub_item import boq_sub_item
else:
	from buildsuite_core.buildsuite_core.doctype.boq_item import boq_item
	from buildsuite_core.buildsuite_core.doctype.boq_sub_item import boq_sub_item


class _StoredDoc(SimpleNamespace):
	def __getattr__(self, _key):
		return None

	def insert(self, **_kwargs):
		return self._persist()

	def save(self, **_kwargs):
		return self._persist()

	def _validate_project_scope(self):
		boq_item.BOQItem._validate_project_scope(self)

	def _persist(self):
		if self.doctype == "BOQ Sub Item":
			boq_sub_item.BOQSubItem.validate(self)
		elif self.doctype == "BOQ Item":
			boq_item.BOQItem.validate(self)
		values = {
			key: value for key, value in vars(self).items()
			if not key.startswith("_") and key != "flags"
		}
		# Document.insert() returns the UNROUNDED object, not a database reload.
		for field in ("rate", "amount", "planned_amount", "actual_amount"):
			if values.get(field) is not None:
				values[field] = Decimal(str(values[field])).quantize(Decimal("0.000000001"))
		self._store.rows[self.doctype, self.name] = values
		return self


class _PricingStore:
	def __init__(self):
		self.rows = {}
		self.flags = AssignmentFlags()
		self.db = SimpleNamespace(get_value=self.get_value)

	def has_permission(self, *_args, **_kwargs):
		return True

	def get_doc(self, doctype, name=None):
		values = dict(doctype) if isinstance(doctype, dict) else dict(self.rows[doctype, name])
		values.setdefault("name", f"ROW-{len(self.rows) + 1}")
		values.setdefault("revision", 1)
		values.setdefault("status", "Draft")
		values.setdefault("margin_rate", 0)
		values.setdefault("tax_rate", 0)
		return _StoredDoc(_store=self, flags=AssignmentFlags(), **values)

	def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None, limit=None):
		rows = [
			row for (kind, _name), row in self.rows.items()
			if kind == doctype and all(row.get(k) == v for k, v in (filters or {}).items())
		]
		if order_by == "revision desc":
			rows.sort(key=lambda row: row["revision"], reverse=True)
		if limit:
			rows = rows[:limit]
		return [row.get(pluck) if pluck else _StoredDoc(**row) for row in rows]

	def get_value(self, doctype, name, fields, as_dict=False):
		row = self.rows.get((doctype, name))
		if row is None:
			return None
		if as_dict:
			return _StoredDoc(**{field: row.get(field) for field in fields})
		return row.get(fields)

	def delete_doc(self, doctype, name, **_kwargs):
		del self.rows[doctype, name]


class TestPersistedBOQSnapshots(unittest.TestCase):
	def setUp(self):
		self.store = _PricingStore()
		for module in (api, boq_item, boq_sub_item):
			self.addCleanup(patch.stopall)
			patch.object(module, "frappe", self.store).start()
		patch.object(api, "recompute_boq").start()

	def _source(self, coefficient=1, rate=100, parts=1, qty=4):
		store = self.store
		store.get_doc({
			"doctype": "Construction Rate Master", "name": "RM-1", "current_rate": rate, "uom": "Nos",
		}).insert()
		store.get_doc({
			"doctype": "BOQ", "name": "BOQ-1", "project": "P-1",
		}).insert()
		store.get_doc({
			"doctype": "BOQ Group", "name": "GROUP-1", "boq": "BOQ-1", "code": "A",
			"group_name": "Civil", "idx_order": 1,
		}).insert()
		store.get_doc({
			"doctype": "Assembly", "name": "ASM-1", "rate_per_unit": 100,
			"components": [
				SimpleNamespace(resource="RM-1", resource_name="Material", coefficient=coefficient)
				for _ in range(parts)
			],
		}).insert()
		store.get_doc({
			"doctype": "BOQ Item", "name": "LINE-1", "boq": "BOQ-1", "boq_group": "GROUP-1",
			"code": "A.1", "description": "Snapshot", "unit": "Nos", "assembly": "ASM-1",
			"planned_qty": qty, "driving_qty": qty, "rate": 0,
			"work_package": None, "cost_head": None,
		}).insert()
		api.explode_item("LINE-1")

	def test_explode_sums_each_persisted_amount_at_nine_decimal_places(self):
		self._source(coefficient=0.333333333, rate=1.000000002, parts=2, qty=10000000)
		line = self.store.get_doc("BOQ Item", "LINE-1")
		amounts = self.store.get_all("BOQ Sub Item", filters={"boq_item": line.name}, pluck="amount")
		self.assertEqual(amounts, [Decimal("0.333333334"), Decimal("0.333333334")])
		self.assertEqual(line.rate, Decimal("0.666666668"))
		self.assertEqual(line.rate, sum(amounts))
		self.assertEqual(line.planned_amount, Decimal("6666666.680000000"))

	def _assert_copy_preserves_snapshot(self, mode):
		self._source()
		self.store.rows["BOQ", "BOQ-1"]["status"] = "Approved"
		self.store.rows["Construction Rate Master", "RM-1"]["current_rate"] = 150
		if mode == "CLONE":
			target = api.clone_boq("P-1", "P-2")["boq"]
		else:
			target = api.create_revision("BOQ-1")
		line = self.store.get_all("BOQ Item", filters={"boq": target})[0]
		parts = self.store.get_all("BOQ Sub Item", filters={"boq_item": line.name})
		self.assertEqual(line.rate, Decimal("100"))
		self.assertEqual([part.rate for part in parts], [Decimal("100")])
		self.assertEqual([part.amount for part in parts], [Decimal("100")])
		self.assertEqual(line.rate, sum(part.amount for part in parts))
		self.assertEqual(parts[0].uom, "Nos")
		self.assertEqual(self.store.get_doc("BOQ Item", "LINE-1").rate, Decimal("100"))
		return line, parts

	def test_clone_keeps_released_snapshot_after_master_rate_change(self):
		self._assert_copy_preserves_snapshot("CLONE")

	def test_revision_keeps_released_snapshot_after_master_rate_change(self):
		self._assert_copy_preserves_snapshot("REVISION")

	def test_clone_keeps_stored_amount_instead_of_recalculating_product(self):
		self._source(coefficient=0.333333333, rate=1.000000002)
		# Seed the previously reconciled, persisted source snapshot independently.
		self.store.rows["BOQ Item", "LINE-1"]["rate"] = Decimal("0.333333334")
		self.store.rows["Construction Rate Master", "RM-1"]["current_rate"] = 2
		target = api.clone_boq("P-1", "P-2")["boq"]
		line = self.store.get_all("BOQ Item", filters={"boq": target})[0]
		part = self.store.get_all("BOQ Sub Item", filters={"boq_item": line.name})[0]
		self.assertEqual(part.rate, Decimal("1.000000002"))
		self.assertEqual(part.amount, Decimal("0.333333334"))
		self.assertEqual(line.rate, part.amount)
