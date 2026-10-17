# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""Selemani subcontract valuation share-locks one project's BOQ rows, then that
BOQ's items. Without native indexes on BOQ.project and BOQ Item.boq those locking
reads scan, and lock, every project's rows, so another project's BOQ approve or
item edit deadlocks. The DocType JSON owns standard-field indexes; bumping
`modified` makes migrate re-import the DocType and add them via updatedb.

Pure metadata assertions: no site, database or Frappe import is needed.
"""

import json
import unittest
from pathlib import Path

DOCTYPES = Path(__file__).resolve().parents[1] / "buildsuite_core" / "doctype"
PREVIOUS_MODIFIED = "2026-06-26 00:00:00.000000"


def _field(doctype, fieldname):
	meta = json.loads((DOCTYPES / doctype / f"{doctype}.json").read_text())
	return meta, next(row for row in meta["fields"] if row["fieldname"] == fieldname)


class TestBOQLockIndexes(unittest.TestCase):
	def test_boq_project_is_search_indexed(self):
		meta, field = _field("boq", "project")
		self.assertEqual(field.get("search_index"), 1)
		self.assertGreater(meta["modified"], PREVIOUS_MODIFIED, "migrate re-imports only a newer DocType")

	def test_boq_item_boq_is_search_indexed(self):
		meta, field = _field("boq_item", "boq")
		self.assertEqual(field.get("search_index"), 1)
		self.assertGreater(meta["modified"], PREVIOUS_MODIFIED, "migrate re-imports only a newer DocType")


if __name__ == "__main__":
	unittest.main()
