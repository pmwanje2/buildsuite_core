"""Explicit native-job wrapper; queue is progress, signed Comment is outcome.

The executor/documents are real application code over the existing BOQ stub.
Only the external Frappe/RQ boundary is doubled; native proof is separate.
"""

import copy
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from buildsuite_core.tests import test_boq_line_values as fixtures

api = fixtures.api


class NativeQueueBoundary:
	def __init__(self, store):
		self.store = store
		self.calls = []
		self.pending = []
		self.jobs = {}
		self.reads = []
		self.current_job = None
		store.local = SimpleNamespace(site="one.example.test")

	def key(self, job_id):
		return (self.store.local.site, job_id)

	def enqueue(
		self,
		method,
		queue="default",
		timeout=None,
		event=None,
		is_async=True,
		job_name=None,
		now=False,
		enqueue_after_commit=False,
		*,
		on_success=None,
		on_failure=None,
		at_front=False,
		job_id=None,
		deduplicate=False,
		at_front_when_starved=False,
		**kwargs,
	):
		self.calls.append(
			dict(
				method=method,
				queue=queue,
				job_id=job_id,
				deduplicate=deduplicate,
				enqueue_after_commit=enqueue_after_commit,
				kwargs=copy.deepcopy(kwargs),
			)
		)
		key = self.key(job_id)
		if deduplicate and self.jobs.get(key, {}).get("status") in ("queued", "started"):
			return None
		job = dict(
			method=method,
			kwargs=copy.deepcopy(kwargs),
			user=self.store.session.user,
			enqueuer=self.store.session.user,
			site=self.store.local.site,
			status="queued",
		)
		if enqueue_after_commit:
			self.pending.append((key, job))
		else:
			self.jobs[key] = job
		return None  # Native enqueue_after_commit deliberately has no Job return.

	def commit(self):
		for key, job in self.pending:
			self.jobs[key] = job
		self.pending.clear()

	def get_job_status(self, job_id):
		self.reads.append(self.key(job_id))
		return self.jobs.get(self.key(job_id), {}).get("status")

	def run(self, job_id):
		job = self.jobs[self.key(job_id)]
		old_user, old_site = self.store.session.user, self.store.local.site
		self.store.session.user, self.store.local.site = job["user"], job["site"]
		job["status"] = "started"
		self.current_job = SimpleNamespace(kwargs={"user": job["enqueuer"], "site": job["site"]})
		try:
			method = job["method"]
			if isinstance(method, str):
				method = getattr(api, method.rsplit(".", 1)[-1])
			result = method(**job["kwargs"])
			job["status"] = "finished"
			return result
		except Exception:
			job["status"] = "failed"
			raise
		finally:
			self.current_job = None
			self.store.session.user, self.store.local.site = old_user, old_site


class TestBOQLineJobs(unittest.TestCase):
	call = fixtures.TestBOQLineValues.call
	preview = fixtures.TestBOQLineValues.preview
	unchanged = fixtures.TestBOQLineValues.unchanged

	def setUp(self):
		fixtures.TestBOQLineValues.setUp(self)
		self.queue = NativeQueueBoundary(self.store)
		self.store.enqueue = self.queue.enqueue
		background = ModuleType("frappe.utils.background_jobs")
		background.get_job_status = self.queue.get_job_status
		patch.dict(sys.modules, {"frappe.utils.background_jobs": background}).start()
		rq = ModuleType("rq")
		rq.get_current_job = lambda: self.queue.current_job
		patch.dict(sys.modules, {"rq": rq}).start()
		self.changes = [{"boq_item": "Z", "planned_qty": "4"}, {"boq_item": "A", "rate": "12"}]
		self.fingerprint = self.preview(self.changes)["fingerprint"]

	def enqueue(self, **kwargs):
		return self.call(
			"enqueue_update_line_values",
			"B",
			kwargs.pop("changes", self.changes),
			kwargs.pop("fingerprint", self.fingerprint),
			kwargs.pop("request_id", "req-1"),
			**kwargs,
		)

	def status(self, **kwargs):
		return self.call("get_line_values_job_status", "B", "req-1", self.fingerprint, **kwargs)

	def test_enqueue_defers_long_queue_with_exact_executor_inputs(self):
		before = copy.deepcopy(self.store.rows)
		result = self.enqueue(edit_other_draft=0)
		self.assertEqual(result["status"], "NOT_APPLIED_SAFE_RETRY")
		self.assertEqual(result["progress"], "PENDING_COMMIT")
		call = self.queue.calls[0]
		self.assertEqual(call["queue"], "long")
		self.assertIs(call["deduplicate"], True)
		self.assertIs(call["enqueue_after_commit"], True)
		self.assertEqual(call["kwargs"]["boq"], "B")
		self.assertEqual(call["kwargs"]["changes"], sorted(self.changes, key=lambda row: row["boq_item"]))
		self.assertEqual(call["kwargs"]["fingerprint"], self.fingerprint)
		self.assertEqual(call["kwargs"]["request_id"], "req-1")
		self.assertEqual(call["kwargs"]["edit_other_draft"], 0)
		self.assertNotIn("user", call["kwargs"])
		self.assertNotIn("role", call["kwargs"])
		self.assertEqual(self.queue.jobs, {})
		self.unchanged(before)

	def test_duplicate_queue_and_committed_retry_never_write_twice(self):
		first = self.enqueue()
		self.queue.commit()
		second = self.enqueue()
		self.assertEqual(first["job_id"], second["job_id"])
		self.assertEqual(len(self.queue.jobs), 1)
		self.queue.run(first["job_id"])
		events = list(self.store.events)
		calls = len(self.queue.calls)
		self.assertEqual(self.enqueue()["status"], "COMMITTED")
		self.assertEqual(len(self.queue.calls), calls)
		self.assertEqual([e for e in self.store.events[len(events) :] if e[0] == "save"], [])
		self.assertEqual(self.store.versions, ["A", "Z"])

	def test_worker_calls_existing_executor_under_native_enqueuer_identity(self):
		result = self.enqueue()
		self.queue.commit()
		self.store.session.user = "other@example.test"
		with patch.object(api, "update_line_values", wraps=api.update_line_values) as executor:
			self.queue.run(result["job_id"])
			self.assertEqual(executor.call_count, 1)
			bound = (
				__import__("inspect")
				.signature(executor._mock_wraps)
				.bind(*executor.call_args.args, **executor.call_args.kwargs)
			)
			self.assertEqual(bound.arguments["test_run"], 0)
			self.assertEqual(bound.arguments["request_id"], "req-1")
			self.assertEqual(bound.arguments["fingerprint"], self.fingerprint)
		self.assertEqual(self.store.rows["BOQ Item", "A"]["rate"], "12.00")
		self.assertEqual(self.store.rows["BOQ Item", "Z"]["planned_qty"], "4.000")
		self.assertEqual(len(self.store.comments), 1)
		self.assertEqual(api.get_line_values_outcome("B", "req-1")["actor"], "qs@example.test")

	def test_native_retry_identity_loss_fails_closed_without_privileged_writes(self):
		job = self.enqueue(edit_other_draft=1)
		self.queue.commit()
		native = self.queue.jobs[self.queue.key(job["job_id"])]
		native["user"] = "Administrator"  # pinned Frappe recursive retry loses user
		before = copy.deepcopy(self.store.rows)
		with self.assertRaisesRegex(PermissionError, "BOQ_LINES_JOB_IDENTITY_MISMATCH"):
			self.queue.run(job["job_id"])
		self.unchanged(before)

	def test_worker_refuses_absent_or_cross_site_native_job_context(self):
		job = self.enqueue()
		self.queue.commit()
		for context in (
			None,
			SimpleNamespace(kwargs={"user": "qs@example.test", "site": "foreign.example.test"}),
		):
			with (
				self.subTest(context=context),
				patch.object(sys.modules["rq"], "get_current_job", return_value=context),
			):
				with self.assertRaisesRegex(PermissionError, "BOQ_LINES_JOB_IDENTITY_MISMATCH"):
					self.queue.run(job["job_id"])
		self.assertEqual(self.store.versions, [])

	def test_worker_result_never_persists_preview_financials_in_rq(self):
		job = self.enqueue()
		self.queue.commit()
		result = self.queue.run(job["job_id"])
		self.assertEqual(result["status"], "COMMITTED")
		self.assertEqual(result["line_count"], 2)
		self.assertTrue({"totals", "lines", "groups", "currency"}.isdisjoint(result))

	def test_no_caller_identity_or_execution_mode_override(self):
		self.call("enqueue_update_line_values", "B", self.changes, self.fingerprint, "req-1")
		for field, value in [
			("user", "Administrator"),
			("role", "System Manager"),
			("test_run", 1),
			("now", True),
		]:
			with self.subTest(field=field), self.assertRaises(TypeError):
				self.enqueue(**{field: value})

	def test_job_namespace_separates_actors_sites_and_delimiter_inputs(self):
		first = self.enqueue(edit_other_draft=1)
		self.queue.commit()
		self.store.session.user = "second@example.test"
		second = self.enqueue(edit_other_draft=1)
		self.assertNotEqual(first["job_id"], second["job_id"])
		self.store.session.user = "qs@example.test"
		self.store.local.site = "two.example.test"
		self.assertEqual(self.status()["progress"], "UNKNOWN")
		self.enqueue(edit_other_draft=1)
		self.queue.commit()
		self.assertEqual(len(self.queue.jobs), 3)
		self.assertNotEqual(
			self.enqueue(request_id="req:1")["job_id"], self.enqueue(request_id="req::1")["job_id"]
		)

	def test_foreign_actor_cannot_adopt_existing_signed_receipt(self):
		job = self.enqueue()
		self.queue.commit()
		self.queue.run(job["job_id"])
		self.store.session.user = "second@example.test"
		for method in (lambda: self.enqueue(edit_other_draft=1), self.status):
			with self.assertRaisesRegex(PermissionError, "BOQ_LINES_REQUEST_ACTOR_MISMATCH"):
				method()
		self.assertEqual(len(self.queue.calls), 1)

	def test_worker_refuses_other_actor_receipt_even_after_queue_race(self):
		first = self.enqueue(edit_other_draft=1)
		self.queue.commit()
		self.store.session.user = "second@example.test"
		second = self.enqueue(edit_other_draft=1)
		self.queue.commit()
		self.queue.run(first["job_id"])
		with self.assertRaisesRegex(PermissionError, "BOQ_LINES_REQUEST_ACTOR_MISMATCH"):
			self.queue.run(second["job_id"])
		self.assertEqual(self.store.versions, ["A", "Z"])

	def test_revoked_boq_item_and_field_permissions_before_worker(self):
		job = self.enqueue()
		self.queue.commit()
		before = copy.deepcopy(self.store.rows)
		for denied in [("BOQ", "B", "write"), ("BOQ Item", "A", "write")]:
			self.store.denied.add(denied)
			with self.assertRaises(PermissionError):
				self.queue.run(job["job_id"])
			self.unchanged(before)
			self.store.denied.clear()
		self.store.denied_fields.add(("Z", "planned_qty"))
		with self.assertRaises(PermissionError):
			self.queue.run(job["job_id"])
		self.unchanged(before)

	def test_stale_and_foreign_line_refused_before_queue(self):
		self.call("get_line_values_job_status", "B", "req-1", self.fingerprint)
		self.store.rows["BOQ Item", "A"]["modified"] = "later"
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_STALE"):
			self.enqueue()
		self.store.rows["BOQ Item", "Z"]["boq"] = "FOREIGN"
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_NOT_IN_BOQ"):
			self.enqueue()
		self.assertEqual(self.queue.calls, [])

	def test_payload_binding_cap_and_invalid_values_refused_before_queue(self):
		self.call("get_line_values_job_status", "B", "req-1", self.fingerprint)
		for changes in [
			self.changes * 501,
			[self.changes[0]] * 2,
			[{"boq_item": "A", "rate": "12.001"}],
			[{"boq_item": "A", "rate": "14"}],
		]:
			with self.subTest(changes=changes[:1]), self.assertRaises(ValueError):
				self.enqueue(changes=changes)
		self.assertEqual(self.queue.calls, [])

	def test_permissions_and_owner_ack_checked_before_queue(self):
		self.call("get_line_values_job_status", "B", "req-1", self.fingerprint)
		self.store.denied_fields.add(("A", "rate"))
		with self.assertRaises(PermissionError):
			self.enqueue()
		self.store.denied_fields.clear()
		self.store.rows["BOQ", "B"]["owner"] = "owner@example.test"
		self.fingerprint = self.preview(self.changes)["fingerprint"]
		with self.assertRaisesRegex(ValueError, "BOQ_DRAFT_OWNER_ACK_REQUIRED"):
			self.enqueue()
		self.assertEqual(self.queue.calls, [])
		self.enqueue(edit_other_draft=1)
		self.queue.commit()
		job_id = self.queue.calls[0]["job_id"]
		self.queue.run(job_id)
		self.assertEqual(api.get_line_values_outcome("B", "req-1")["edit_other_draft"], 1)

	def test_live_worker_state_changes_still_refuse(self):
		job = self.enqueue()
		self.queue.commit()
		for key, value in [("status", "Approved"), ("modified", "later"), ("owner", "new@example.test")]:
			with self.subTest(key=key):
				old = self.store.rows["BOQ", "B"][key]
				self.store.rows["BOQ", "B"][key] = value
				before = copy.deepcopy(self.store.rows)
				with self.assertRaises(ValueError):
					self.queue.run(job["job_id"])
				self.unchanged(before)
				self.store.rows["BOQ", "B"][key] = old

	def test_failure_after_first_save_rolls_back_every_line_and_receipt(self):
		job = self.enqueue()
		self.queue.commit()
		before = copy.deepcopy(self.store.rows)
		self.store.fail_save = "Z"
		with self.assertRaisesRegex(ValueError, "native validation refused"):
			self.queue.run(job["job_id"])
		self.assertTrue(any(e[:3] == ("save", "BOQ Item", "A") for e in self.store.events))
		self.unchanged(before)
		status = self.status()
		self.assertEqual(status["progress"], "FAILED")
		self.assertEqual(status["status"], "NOT_APPLIED_SAFE_RETRY")

	def test_rq_states_are_progress_only_including_finished_and_expired(self):
		job = self.enqueue()
		self.queue.commit()
		for native, progress in [
			("queued", "QUEUED"),
			("started", "RUNNING"),
			("failed", "FAILED"),
			("finished", "UNKNOWN"),
		]:
			with self.subTest(native=native):
				self.queue.jobs[self.queue.key(job["job_id"])]["status"] = native
				status = self.status()
				self.assertEqual(status["progress"], progress)
				self.assertEqual(status["status"], "NOT_APPLIED_SAFE_RETRY")
		self.queue.jobs.clear()
		self.assertEqual(self.status()["progress"], "UNKNOWN")
		self.assertEqual(self.status()["status"], "NOT_APPLIED_SAFE_RETRY")
		self.store.rows["BOQ Item", "A"]["modified"] = "later"
		self.assertEqual(self.status()["status"], "STALE_REPREVIEW")

	def test_signed_comment_proves_success_even_after_job_expires(self):
		job = self.enqueue()
		self.queue.commit()
		self.queue.run(job["job_id"])
		self.queue.jobs.clear()
		status = self.status()
		self.assertEqual(status["status"], "COMMITTED")
		self.assertEqual(status["line_count"], 2)
		self.assertEqual(status["progress"], "UNKNOWN")
		self.assertTrue({"totals", "lines", "groups", "currency"}.isdisjoint(status))

	def test_forged_comment_and_changed_payload_do_not_gain_authority(self):
		job = self.enqueue()
		self.queue.commit()
		self.queue.run(job["job_id"])
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_STALE"):
			self.enqueue(changes=[{"boq_item": "A", "rate": "14"}])
		self.store.comments[0]["content"] = json.dumps(
			{
				"contract": "BOQLineValues.v1",
				"request_id": "req-1",
				"outcome": {"boq": "B", "status": "COMMITTED"},
			}
		)
		self.assertEqual(self.status()["status"], "STALE_REPREVIEW")

	def test_status_permission_precedes_rq_read_and_never_discloses_financials(self):
		job = self.enqueue()
		self.queue.commit()
		self.queue.run(job["job_id"])
		self.store.denied.add(("BOQ", "B", "read"))
		reads = len(self.queue.reads)
		with self.assertRaises(PermissionError):
			self.status()
		self.assertEqual(len(self.queue.reads), reads)
		self.store.denied.clear()
		self.store.denied_read_fields.add(("A", "rate"))
		status = self.status()
		self.assertEqual(status["status"], "COMMITTED")
		self.assertTrue({"totals", "lines", "groups", "currency"}.isdisjoint(status))

	def test_wrapper_is_explicit_post_only_and_worker_not_whitelisted(self):
		self.enqueue()
		self.assertEqual(api.enqueue_update_line_values._http_methods, ["POST"])
		worker = self.queue.calls[0]["method"]
		if isinstance(worker, str):
			worker = getattr(api, worker.rsplit(".", 1)[-1])
		self.assertFalse(getattr(worker, "_whitelisted", False))
		self.assertEqual(len(self.store.comments), 0)


if __name__ == "__main__":
	unittest.main()
