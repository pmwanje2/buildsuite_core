"""Native RQ proof, explicitly admitted only on this lane's disposable bench.

Run from bench sites with its Python, SOURCE_SHA and BOQ_API_SHA256 set.
This is command/native proof, not a frontend or production-user UX test.
"""

import hashlib
import inspect
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.utils.background_jobs import create_job_id, get_job, get_queue

from buildsuite_core.api import boq as api

SITE = "wp17-jobs6154.localhost"
ROLE = "WP17 JOBS TEST QS"
USER = "wp17-jobs-qs@example.test"
OTHER = "wp17-jobs-other@example.test"


def insert(doctype, **fields):
	return frappe.get_doc(dict(doctype=doctype, **fields)).insert(ignore_permissions=True)


class NativeLineJobs(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		assert frappe.local.site == SITE
		assert frappe.conf.db_host == "wp17-jobs6154-db"
		assert frappe.get_all("Company", pluck="name") in ([], ["WP17 JOBS TEST Company"])
		assert hashlib.sha256(Path(api.__file__).read_bytes()).hexdigest() == os.environ["BOQ_API_SHA256"]
		print(
			"SOURCE_RECEIPT",
			json.dumps(
				{
					"source_sha": os.environ["SOURCE_SHA"],
					"api_sha256": os.environ["BOQ_API_SHA256"],
					"frappe": frappe.__version__,
					"python": sys.version.split()[0],
					"enqueue_signature": str(inspect.signature(frappe.enqueue)),
				}
			),
			flush=True,
		)
		frappe.set_user("Administrator")
		frappe.db.set_single_value("System Settings", "currency_precision", "2")
		frappe.db.set_single_value("System Settings", "float_precision", "3")
		if not frappe.db.exists("Warehouse Type", "Transit"):
			insert("Warehouse Type", name="Transit")
		if not frappe.db.exists("UOM", "Nos"):
			insert("UOM", uom_name="Nos")
		if not frappe.db.exists("Company", "WP17 JOBS TEST Company"):
			insert(
				"Company",
				company_name="WP17 JOBS TEST Company",
				abbr="WJ17",
				country="Uganda",
				default_currency="USD",
			)
		if not frappe.db.exists("Role", ROLE):
			insert("Role", role_name=ROLE)
		for dt in ("BOQ", "BOQ Item", "BOQ Group", "Project", "Company", "UOM"):
			add_permission(dt, ROLE, 0)
			update_permission_property(dt, ROLE, 0, "read", 1)
			if dt in ("BOQ", "BOQ Item"):
				update_permission_property(dt, ROLE, 0, "write", 1)
		for user in (USER, OTHER):
			if not frappe.db.exists("User", user):
				insert(
					"User", email=user, first_name="TEST JOB QS", send_welcome_email=0, roles=[{"role": ROLE}]
				)
		frappe.clear_cache()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		suffix = frappe.generate_hash(length=8)
		project = insert(
			"Project",
			project_name="WP17 JOBS TEST " + suffix,
			custom_project_id="JOBS-" + suffix,
			company="WP17 JOBS TEST Company",
		)
		base = insert("BOQ", project=project.name, title="TEST JOB base", status="Approved")
		frappe.set_user(USER)
		self.boq = insert("BOQ", project=project.name, title="TEST JOB revision", base_revision=base.name)
		group = insert("BOQ Group", boq=self.boq.name, code="1", group_name="TEST JOB bill")
		self.items = sorted(
			[
				insert(
					"BOQ Item",
					boq=self.boq.name,
					boq_group=group.name,
					code=str(i),
					description="TEST JOB concrete",
					unit="Nos",
					rate=10,
					planned_qty=2,
				)
				for i in range(2)
			],
			key=lambda item: item.name,
		)
		self.request = "native-job-" + suffix
		self.changes = [{"boq_item": item.name, "rate": "12"} for item in self.items]
		self.fingerprint = api.preview_update_line_values(self.boq.name, self.changes, self.request)[
			"fingerprint"
		]
		frappe.db.commit()
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(lambda: frappe.set_user("Administrator"))

	def enqueue(self, **kwargs):
		return api.enqueue_update_line_values(
			self.boq.name, self.changes, self.fingerprint, self.request, **kwargs
		)

	def status(self):
		return api.get_line_values_job_status(self.boq.name, self.request, self.fingerprint)

	def work(self, preamble=""):
		frappe.db.commit()
		code = """import frappe
from rq import SimpleWorker
from frappe.utils.background_jobs import get_queue, get_redis_conn
frappe.init(site='wp17-jobs6154.localhost', sites_path='/home/frappe/frappe-bench/sites')
frappe.connect()
assert frappe.conf.db_host == 'wp17-jobs6154-db'
"""
		code += (
			preamble
			+ "\nSimpleWorker([get_queue('long')], connection=get_redis_conn()).work(burst=True, logging_level='WARNING')\nfrappe.destroy()\n"
		)
		result = subprocess.run(
			[sys.executable, "-B", "-c", code], text=True, capture_output=True, timeout=60
		)
		self.assertEqual(result.returncode, 0, result.stderr[-3000:])
		frappe.db.rollback()
		frappe.clear_cache()
		frappe.set_user(USER)
		return result.stdout + result.stderr

	def values(self):
		return [frappe.db.get_value("BOQ Item", item.name, "rate") for item in self.items]

	def test_native_after_commit_dedup_identity_commit_versions_and_expiry(self):
		job = self.enqueue()
		self.assertIsNone(get_job(job["job_id"]))
		frappe.db.commit()
		native = get_job(job["job_id"])
		self.assertEqual(native.origin, get_queue("long").name)
		self.assertEqual(native.kwargs["user"], USER)
		self.assertEqual(native.kwargs["site"], SITE)
		self.assertEqual(native.kwargs["kwargs"]["request_id"], self.request)
		self.assertEqual(native.kwargs["kwargs"]["fingerprint"], self.fingerprint)
		self.assertNotIn("user", native.kwargs["kwargs"])
		self.assertEqual(self.status()["progress"], "QUEUED")
		self.assertEqual(self.enqueue()["job_id"], job["job_id"])
		frappe.db.commit()
		self.assertEqual(get_queue("long").job_ids.count(create_job_id(job["job_id"])), 1)
		self.work()
		self.assertEqual(self.values(), [12, 12])
		versions = frappe.get_all(
			"Version",
			filters={"ref_doctype": "BOQ Item", "docname": ["in", [item.name for item in self.items]]},
			fields=["owner"],
		)
		self.assertEqual(len(versions), 2)
		self.assertTrue(all(row.owner == USER for row in versions))
		self.assertEqual(self.status()["status"], "COMMITTED")
		self.assertEqual(self.enqueue()["status"], "COMMITTED")
		self.assertTrue({"totals", "lines", "groups", "currency"}.isdisjoint(native.return_value()))
		get_job(job["job_id"]).delete()
		self.assertEqual(self.status()["status"], "COMMITTED")
		self.assertEqual(self.status()["progress"], "UNKNOWN")
		self.assertTrue({"totals", "lines", "groups", "currency"}.isdisjoint(self.status()))
		print(
			"NATIVE_JOB_RECEIPT",
			json.dumps(
				{
					"status": "COMMITTED",
					"actor": USER,
					"versions": len(versions),
					"duplicate_queue_entries": 0,
					"expired_job_progress": "UNKNOWN",
				}
			),
			flush=True,
		)

	def test_native_rollback_does_not_enqueue(self):
		job = self.enqueue()
		frappe.db.rollback()
		self.assertIsNone(get_job(job["job_id"]))
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		self.assertEqual(self.values(), [10, 10])

	def test_native_expired_and_finished_jobs_are_not_commit_proof(self):
		job = self.enqueue()
		frappe.db.commit()
		native = get_job(job["job_id"])
		native.set_status("finished")
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		self.assertEqual(self.status()["progress"], "UNKNOWN")
		native.delete()
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		frappe.set_user("Administrator")
		frappe.db.set_value("BOQ Item", self.items[0].name, "rate", 11)
		frappe.db.commit()
		frappe.set_user(USER)
		self.assertEqual(self.status()["status"], "STALE_REPREVIEW")

	def test_native_worker_revoked_write_permissions(self):
		job = self.enqueue()
		frappe.db.commit()
		frappe.set_user("Administrator")
		update_permission_property("BOQ Item", ROLE, 0, "write", 0)
		frappe.clear_cache()
		frappe.db.commit()
		try:
			self.work()
			self.assertEqual(self.values(), [10, 10])
			self.assertEqual(get_job(job["job_id"]).get_status().value, "failed")
			self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		finally:
			frappe.set_user("Administrator")
			update_permission_property("BOQ Item", ROLE, 0, "write", 1)
			frappe.clear_cache()
			frappe.db.commit()

	def test_native_retry_loses_identity_refuses_then_explicit_retry_commits_once(self):
		job = self.enqueue(edit_other_draft=1)
		frappe.db.commit()
		self.work("""from buildsuite_core.api import boq as api
def force_retry(*args, **kwargs):
    raise frappe.RetryBackgroundJobError('TEST_NATIVE_RETRY_IDENTITY')
api.update_line_values = force_retry
""")
		native = get_job(job["job_id"])
		self.assertEqual(native.get_status().value, "failed")
		self.assertIn("BOQ_LINES_JOB_IDENTITY_MISMATCH", native.exc_info)
		self.assertEqual(self.values(), [10, 10])
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		self.enqueue(edit_other_draft=1)
		frappe.db.commit()
		self.work()
		self.assertEqual(self.values(), [12, 12])
		self.assertEqual(self.status()["status"], "COMMITTED")
		print(
			"NATIVE_RETRY_IDENTITY",
			"refused elevated recursive retry; explicit actor retry COMMITTED",
			flush=True,
		)

	def test_native_partial_write_failure_rolls_back_versions_and_comment(self):
		frappe.set_user("Administrator")
		insert(
			"Server Script",
			name="TEST JOB partial " + self.request,
			script_type="DocType Event",
			reference_doctype="BOQ Item",
			doctype_event="Before Save",
			disabled=0,
			script=f"if doc.name == {self.items[1].name!r}:\n    frappe.throw('TEST_JOB_SECOND_LINE')",
		)
		frappe.cache.delete_value("server_script_map")
		frappe.db.commit()
		frappe.set_user(USER)
		job = self.enqueue()
		frappe.db.commit()
		trace = self.work("""from buildsuite_core.buildsuite_core.doctype.boq_item.boq_item import BOQItem
original = BOQItem.on_update
def trace_save(doc):
    print('NATIVE_JOB_SAVED', doc.name, flush=True)
    return original(doc)
BOQItem.on_update = trace_save
""")
		self.assertIn("NATIVE_JOB_SAVED " + self.items[0].name, trace)
		self.assertEqual(self.values(), [10, 10])
		self.assertEqual(get_job(job["job_id"]).get_status().value, "failed")
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		self.assertEqual(
			frappe.db.count(
				"Version", {"ref_doctype": "BOQ Item", "docname": ["in", [item.name for item in self.items]]}
			),
			0,
		)
		self.assertEqual(
			frappe.db.count(
				"Comment",
				{
					"reference_doctype": "BOQ",
					"reference_name": self.boq.name,
					"content": ["like", "%BOQLineValues.v1%"],
				},
			),
			0,
		)
		print(
			"NATIVE_PARTIAL_ROLLBACK",
			"first native save observed; 0 persisted values/Versions/outcome Comments",
			flush=True,
		)

	def test_native_live_state_change_refuses_worker(self):
		job = self.enqueue()
		frappe.db.commit()
		frappe.set_user("Administrator")
		frappe.db.set_value("BOQ", self.boq.name, "status", "Submitted")
		frappe.db.commit()
		self.work()
		self.assertEqual(get_job(job["job_id"]).get_status().value, "failed")
		self.assertEqual(self.values(), [10, 10])
		self.assertEqual(self.status()["status"], "STALE_REPREVIEW")

	def test_native_other_actor_namespace_and_receipt_refusal(self):
		first = self.enqueue(edit_other_draft=1)
		frappe.db.commit()
		frappe.set_user(OTHER)
		second = self.enqueue(edit_other_draft=1)
		self.assertNotEqual(first["job_id"], second["job_id"])
		frappe.db.commit()
		self.work()
		self.assertEqual(get_job(second["job_id"]).get_status().value, "failed")
		self.assertEqual(self.values(), [12, 12])
		frappe.set_user(OTHER)
		with self.assertRaises(frappe.PermissionError):
			self.status()

	def test_native_foreign_stale_and_cap_refused_without_jobs(self):
		self.assertTrue(
			callable(getattr(api, "enqueue_update_line_values", None)), "missing native job wrapper"
		)
		before = set(get_queue("long").job_ids)
		with self.assertRaises(frappe.ValidationError):
			api.enqueue_update_line_values(
				self.boq.name, [{"boq_item": "FOREIGN", "rate": "1"}], self.fingerprint, self.request
			)
		with self.assertRaises(frappe.ValidationError):
			api.enqueue_update_line_values(
				self.boq.name,
				[{"boq_item": "LINE" + str(i), "rate": "1"} for i in range(1001)],
				self.fingerprint,
				self.request,
			)
		with self.assertRaises(frappe.ValidationError):
			api.enqueue_update_line_values(self.boq.name, self.changes, "stale", self.request)
		frappe.db.commit()
		self.assertEqual(set(get_queue("long").job_ids), before)

	def test_native_site_namespace_and_read_permission(self):
		job = self.enqueue()
		frappe.db.commit()
		first = create_job_id(job["job_id"])
		with patch.object(frappe.local, "site", "other-jobs6154.localhost"):
			self.assertNotEqual(first, create_job_id(job["job_id"]))
			self.assertIsNone(get_job(job["job_id"]))
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self.status()
		# No worker remains running; discard just this synthetic queued job.
		get_job(job["job_id"]).delete()


if __name__ == "__main__":
	frappe.init(site=SITE, sites_path="/home/frappe/frappe-bench/sites")
	frappe.connect()
	try:
		assert frappe.conf.db_host == "wp17-jobs6154-db", "REFUSE non-owned database"
		unittest.main()
	finally:
		frappe.destroy()
