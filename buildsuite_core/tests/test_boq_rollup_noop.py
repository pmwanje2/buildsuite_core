# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""BOQ header rollup after a BOQ Item save (Desk edit or one Data Import row).

Real BOQItem.on_update, recompute_boq and compute_boq_totals; only the documents
and decimal(21,9) Currency storage are doubled. Re-saving an unchanged header
writes an empty-diff Version and replaces every WP summary row with new names.
"""

import sys
import unittest
from decimal import ROUND_HALF_UP, Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from buildsuite_core.tests._boq_stub import AssignmentFlags, ensure_frappe_stub

ensure_frappe_stub()

# Import before the sys.modules patch below so boq_item binds this same module.
from buildsuite_core.buildsuite_core.doctype.boq import boq_rollup

try:
	from frappe.model.document import Document
except ModuleNotFoundError:
	document_module = ModuleType("frappe.model.document")
	document_module.Document = object
	with patch.dict(sys.modules, {"frappe.model.document": document_module}):
		from buildsuite_core.buildsuite_core.doctype.boq_item import boq_item
else:
	from buildsuite_core.buildsuite_core.doctype.boq_item import boq_item


def _stored(value):
	# Currency columns are decimal(21,9), HALF_UP; Frappe loads them back as float.
	return float(Decimal(str(value)).quantize(Decimal("1e-9"), rounding=ROUND_HALF_UP))


class _Row(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


class _Header(_Row):
	"""A BOQ loaded from the database. save() runs BOQ.validate, then persists."""

	def is_new(self):
		return False

	def set(self, key, value):
		setattr(self, key, list(value))

	def append(self, key, value):
		getattr(self, key).append(_Row(name=None, **value))

	def validate(self):
		# BOQ.validate: anchor company to the project, then roll up totals.
		self.company = self._store.project_company
		boq_rollup.compute_boq_totals(self)

	def save(self, **kwargs):
		store = self._store
		self.validate()
		store.saves.append(kwargs)
		rows = []
		for row in self.wp_summaries:
			# A row without a name is a new child row: Frappe inserts it with a new name.
			store.row_counter += 1
			name = row.name or f"WP-ROW-{store.row_counter}"
			rows.append({"name": name, **{f: row.get(f) for f in store.WP_FIELDS}})
		store.header = {
			"name": self.name,
			"company": self.company,
			"margin_rate": self.margin_rate,
			"tax_rate": self.tax_rate,
			**{f: _stored(self.get(f)) for f in store.TOTALS},
			"wp_summaries": [
				{**row, **{f: _stored(row[f]) for f in store.TOTALS if f in row}} for row in rows
			],
		}
		return self


class _Store:
	TOTALS = ("planned_amount", "actual_amount", "margin_amount", "tax_amount", "total")
	WP_FIELDS = ("work_package", "planned_amount", "margin_amount", "tax_amount", "total")

	def __init__(self):
		self.flags = AssignmentFlags()
		self.saves = []
		self.row_counter = 0
		self.project_company = "TEST Company"
		# Amounts whose margin/tax carry more than nine decimals before storage.
		self.items = {
			"I-1": {
				"boq": "B-1",
				"planned_amount": 100.123456789,
				"actual_amount": 0,
				"work_package": "WP-A",
			},
			"I-2": {"boq": "B-1", "planned_amount": 33.333333333, "actual_amount": 0, "work_package": "WP-B"},
			"I-3": {"boq": "B-1", "planned_amount": 7.7, "actual_amount": 1.1, "work_package": None},
		}
		self.db = SimpleNamespace(
			exists=lambda doctype, name: doctype == "BOQ" and name == "B-1",
			set_value=lambda *args, **kwargs: None,
		)
		self.header = {
			"name": "B-1",
			"company": "TEST Company",
			"margin_rate": 7.25,
			"tax_rate": 18,
			"wp_summaries": [],
		}

	def get_doc(self, doctype, name):
		assert (doctype, name) == ("BOQ", "B-1"), (doctype, name)
		stored = dict(self.header)
		rows = [_Row(**row) for row in stored.pop("wp_summaries")]
		return _Header(_store=self, wp_summaries=rows, **stored)

	def get_all(self, doctype, filters=None, fields=None, **kwargs):
		assert doctype == "BOQ Item", doctype
		return [
			_Row(**{f: item.get(f) for f in fields})
			for item in self.items.values()
			if item["boq"] == (filters or {}).get("boq")
		]

	def row_names(self):
		return [row["name"] for row in self.header["wp_summaries"]]


class TestBOQRollupNoop(unittest.TestCase):
	def setUp(self):
		self.store = _Store()
		self.addCleanup(patch.stopall)
		patch.object(boq_rollup, "frappe", self.store).start()
		patch.object(boq_item, "frappe", self.store).start()
		# The stored header as the last rollup left it.
		self.store.get_doc("BOQ", "B-1").save(ignore_permissions=True)
		self.store.saves.clear()

	def on_update(self, name, **changes):
		self.store.items[name].update(changes)
		item = SimpleNamespace(name=name, boq="B-1", work_package=None, cost_head=None)
		boq_item.BOQItem.on_update(item)

	def test_takeoff_only_rows_do_not_resave_the_header(self):
		rows = self.store.row_names()
		header = dict(self.store.header)
		# One Data Import "Update Existing Records" run of three take-off rows.
		for name in ("I-1", "I-2", "I-3"):
			self.on_update(name, custom_takeoff_qty=12.5)
		self.assertEqual(self.store.saves, [], "unchanged header must not be saved (no empty Version)")
		self.assertEqual(self.store.row_names(), rows, "WP summary rows must keep their names")
		self.assertEqual(self.store.header, header)

	def test_changed_amount_still_saves_with_new_totals(self):
		self.on_update("I-1", planned_amount=200)
		self.assertEqual(self.store.saves, [{"ignore_permissions": True}])
		self.assertEqual(self.store.header["planned_amount"], _stored(200 + 33.333333333 + 7.7))

	def test_moved_work_package_saves_even_with_equal_totals(self):
		total = self.store.header["total"]
		self.on_update("I-3", work_package="WP-A")
		self.assertEqual(len(self.store.saves), 1)
		self.assertEqual(self.store.header["total"], total)
		self.assertEqual([row["work_package"] for row in self.store.header["wp_summaries"]], ["WP-A", "WP-B"])

	def test_reanchored_company_saves(self):
		self.store.project_company = "Other TEST Company"
		self.on_update("I-2", custom_takeoff_qty=1)
		self.assertEqual(len(self.store.saves), 1)
		self.assertEqual(self.store.header["company"], "Other TEST Company")

	def test_skip_flag_still_defers_the_rollup(self):
		self.store.flags.boq_skip_rollup = True
		self.on_update("I-1", planned_amount=500)
		self.assertEqual(self.store.saves, [])


if __name__ == "__main__":
	unittest.main()
