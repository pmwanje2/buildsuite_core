"""Explicit disposable-bench tests, not part of stub-based unittest discovery.

Run as a script from the bench sites directory. Only the synthetic WP17 site
and isolated grok-pr14 database are admitted; no production target is accepted.
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, update_permission_property

from buildsuite_core.api import boq as api


def insert(doctype, **fields):
	return frappe.get_doc(dict(doctype=doctype, **fields)).insert(ignore_permissions=True)


class NativeAmendLineValues(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		assert frappe.get_all("Company", pluck="name") == ["WP17 TEST Company"]
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields(
			{"Assembly": [{"fieldname": "workflow_state", "label": "TEST approval", "fieldtype": "Data"}]}
		)
		add_permission("Assembly", "WP17 TEST QS", 0)
		update_permission_property("Assembly", "WP17 TEST QS", 0, "read", 1)
		frappe.clear_cache()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(lambda: frappe.set_user("Administrator"))
		suffix = frappe.generate_hash(length=8)
		project = insert(
			"Project",
			project_name="WP17 TEST AMEND " + suffix,
			custom_project_id="AMEND-" + suffix,
			company="WP17 TEST Company",
		)
		base = insert("BOQ", project=project.name, title="TEST base", status="Approved")
		frappe.set_user("wp17-qs@example.test")
		self.boq = insert("BOQ", project=project.name, title="TEST revision", base_revision=base.name)
		group = insert("BOQ Group", boq=self.boq.name, code="1", group_name="TEST AMEND")
		self.items = sorted(
			[
				insert(
					"BOQ Item",
					boq=self.boq.name,
					boq_group=group.name,
					code=str(index),
					description="TEST AMEND",
					unit="Nos",
					rate=10,
					planned_qty=2,
				)
				for index in range(2)
			],
			key=lambda item: item.name,
		)
		self.assembly = insert(
			"Assembly",
			assembly_code="TEST-AMEND-" + suffix,
			assembly_name="TEST AMEND",
			uom="Nos",
			disabled=0,
			workflow_state="Draft",
		)

	def preview(self, changes):
		return api.preview_update_line_values(self.boq.name, changes, "amend-native")

	def commit(self, changes, preview):
		return api.update_line_values(
			self.boq.name, changes, preview["fingerprint"], "amend-native", test_run=0
		)

	def test_native_half_cent_8909_13_times_878_500(self):
		item = self.items[0]
		item.planned_qty = 878.5
		item.save()
		changes = [{"boq_item": item.name, "rate": "8909.13"}]
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["after"]["planned_amount"], "7826670.70")
		self.assertEqual(self.commit(changes, preview)["status"], "COMMITTED")
		self.assertEqual(
			Decimal(str(frappe.db.get_value("BOQ Item", item.name, "planned_amount"))).quantize(
				Decimal("0.01")
			),
			Decimal("7826670.70"),
		)

	def test_native_assembly_missing_disabled_and_buildup_refused(self):
		changes = [{"boq_item": self.items[0].name, "rate": "12", "assembly": "TEST-MISSING"}]
		self.assertEqual(self.preview(changes)["lines"][0]["reason"], "BOQ_LINE_ASSEMBLY_NOT_FOUND")
		changes[0]["assembly"] = self.assembly.name
		frappe.db.set_value("Assembly", self.assembly.name, "disabled", 1)
		self.assertEqual(self.preview(changes)["lines"][0]["reason"], "BOQ_LINE_ASSEMBLY_DISABLED")
		frappe.db.set_value("Assembly", self.assembly.name, "disabled", 0)
		frappe.db.set_value("BOQ Item", self.items[0].name, "assembly", self.assembly.name)
		self.assertEqual(self.preview(changes)["lines"][0]["reason"], "BOQ_LINE_RATE_FROM_BUILDUP")

	def test_native_tenant_before_save_governance_rolls_back_whole_batch(self):
		frappe.set_user("Administrator")
		insert(
			"Server Script",
			name="TEST AMEND assembly approval",
			script_type="DocType Event",
			reference_doctype="BOQ Item",
			doctype_event="Before Save",
			disabled=0,
			script='if doc.assembly and frappe.db.get_value("Assembly", doc.assembly, "workflow_state") != "Approved":\n    frappe.throw("TEST_ASSEMBLY_NOT_APPROVED")',
		)
		frappe.cache.delete_value("server_script_map")
		self.addCleanup(lambda: frappe.cache.delete_value("server_script_map"))
		frappe.set_user("wp17-qs@example.test")
		changes = [
			{"boq_item": self.items[0].name, "rate": "12"},
			{"boq_item": self.items[1].name, "rate": "13", "assembly": self.assembly.name},
		]
		preview = self.preview(changes)
		self.assertTrue(all(line["status"] == "OK" for line in preview["lines"]))
		versions = frappe.db.count("Version", {"ref_doctype": "BOQ Item"})
		from buildsuite_core.buildsuite_core.doctype.boq_item.boq_item import BOQItem

		original = BOQItem.on_update
		saved = []

		def record_save(doc):
			saved.append(doc.name)
			original(doc)

		with (
			patch.object(frappe, "get_common_site_config", return_value={"server_script_enabled": True}),
			patch.object(BOQItem, "on_update", record_save),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "TEST_ASSEMBLY_NOT_APPROVED"):
				self.commit(changes, preview)
		self.assertEqual(saved, [self.items[0].name])
		self.assertEqual(frappe.db.count("Version", {"ref_doctype": "BOQ Item"}), versions)
		for item in self.items:
			self.assertEqual(frappe.db.get_value("BOQ Item", item.name, "rate"), 10)
			self.assertFalse(frappe.db.get_value("BOQ Item", item.name, "assembly"))
		self.assertEqual(frappe.db.get_value("BOQ", self.boq.name, "planned_amount"), 40)
		self.assertEqual(
			frappe.db.count("Comment", {"reference_name": self.boq.name, "comment_type": "Comment"}), 0
		)

	def test_native_outcome_works_in_read_only_maintenance_transaction(self):
		changes = [{"boq_item": self.items[0].name, "rate": "12"}]
		preview = self.preview(changes)
		# Keep the synthetic fixture visible after restarting as READ ONLY.
		frappe.db.commit()
		frappe.db.begin(read_only=True)
		with patch.dict(frappe.flags, {"read_only": True}):
			self.assertEqual(
				api.get_line_values_outcome(self.boq.name, "amend-native", preview["fingerprint"])["status"],
				"NOT_APPLIED_SAFE_RETRY",
			)

	def test_native_outcome_recovers_other_connections_commit_after_old_snapshot(self):
		changes = [{"boq_item": self.items[0].name, "rate": "12"}]
		preview = self.preview(changes)
		frappe.db.commit()
		self.assertEqual(
			frappe.db.count("Comment", {"reference_name": self.boq.name, "comment_type": "Comment"}), 0
		)
		apply = subprocess.run(
			[
				sys.executable,
				"-B",
				"-c",
				f"""import frappe
frappe.init(site='wp17-6154-test.localhost', sites_path='/home/frappe/frappe-bench/sites')
assert frappe.conf.db_host == 'grok-pr14-suth77-db'
frappe.connect()
frappe.set_user('wp17-qs@example.test')
from buildsuite_core.api.boq import update_line_values
assert update_line_values({self.boq.name!r}, {changes!r}, {preview["fingerprint"]!r}, 'amend-native', test_run=0)['status'] == 'COMMITTED'
frappe.db.commit()
frappe.destroy()
""",
			],
			capture_output=True,
			text=True,
			timeout=30,
		)
		self.assertEqual(apply.returncode, 0, apply.stdout + apply.stderr)
		try:
			outcome = api.get_line_values_outcome(self.boq.name, "amend-native")
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			outcome = api.get_line_values_outcome(self.boq.name, "amend-native")
		self.assertEqual(outcome["status"], "COMMITTED")


if __name__ == "__main__":
	frappe.init(site="wp17-6154-test.localhost", sites_path="/home/frappe/frappe-bench/sites")
	assert frappe.conf.db_host == "grok-pr14-suth77-db"
	assert "grok-pr14-suth77-redis" in frappe.conf.redis_cache
	frappe.connect()
	try:
		source = Path(api.__file__)
		digest = hashlib.sha256(source.read_bytes()).hexdigest()
		assert digest == os.environ["AMEND_EXPECTED_BOQ_SHA256"]
		print(
			"SOURCE_STAMP",
			json.dumps(
				{"head": os.environ["AMEND_EXPECTED_HEAD"], "boq_sha256": digest, "source": str(source)}
			),
			flush=True,
		)
		unittest.main(verbosity=2)
	finally:
		frappe.db.rollback()
		frappe.destroy()
