"""Atomic BOQ edits: real executor, doubled Frappe documents/transaction boundary.

These tests catch partial writes, silent permlevel resets, rounding drift,
unlocked stale checks and treating an unknown job as a committed transaction.
Native Version creation still requires the disposable-bench receipt.
"""

import copy
import hashlib
import hmac
import html
import json
import sys
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from buildsuite_core.tests._boq_stub import AssignmentFlags, ensure_frappe_stub

ensure_frappe_stub()

from buildsuite_core.api import boq as api


class LineDoc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def precision(self, fieldname):
		return self._store.precisions.get(fieldname, 2)

	def has_permlevel_access_to(self, fieldname, df=None, permission_type="read"):
		assert permission_type in ("write", "read")
		denied = self._store.denied_fields if permission_type == "write" else self._store.denied_read_fields
		return (self.name, fieldname) not in denied

	def save(self, **kwargs):
		assert not kwargs, "native save must retain permission and Version machinery"
		store = self._store
		store.events.append(("save", self.doctype, self.name, store.flags.get("boq_skip_rollup")))
		if self.name == store.fail_save:
			raise ValueError("native validation refused")
		if self.doctype == "BOQ Item":
			# BOQItem.validate uses flt(qty) * flt(rate), then decimal(21,9) storage.
			self.planned_amount = str(
				Decimal(str(float(self.planned_qty) * float(self.rate))).quantize(
					Decimal("1e-9"), rounding=ROUND_HALF_UP
				)
			)
			if self.name == store.reset_save:
				self.rate = "10.00"
			store.versions.append(self.name)
		self.modified = "2026-10-28 12:01:00"
		store.rows[self.doctype, self.name] = {
			key: value for key, value in vars(self).items() if not key.startswith("_")
		}
		return self

	def add_comment(self, comment_type, text=None):
		assert comment_type == "Comment"
		self._store.comments.append(
			{
				"name": f"COMMENT-{len(self._store.comments)}",
				"comment_type": comment_type,
				"reference_doctype": "BOQ",
				"reference_name": self.name,
				"content": text,
			}
		)
		if self._store.fail_comment:
			raise ValueError("comment failed")
		return SimpleNamespace(name=self._store.comments[-1]["name"])


class LineStore:
	def __init__(self):
		self.rows = {}
		self.events = []
		self.comments = []
		self.versions = []
		self.denied = set()
		self.denied_fields = set()
		self.denied_read_fields = set()
		self.precisions = {"planned_qty": 3}
		self.flags = AssignmentFlags()
		self.session = SimpleNamespace(user="qs@example.test")
		self.PermissionError = PermissionError
		self.ValidationError = ValueError
		self.DoesNotExistError = LookupError
		self.fail_save = self.reset_save = None
		self.fail_comment = False
		self.on_lock = None
		self.db = SimpleNamespace(
			savepoint=self.savepoint,
			rollback=self.rollback,
			get_value=self.get_value,
			count=self.count,
			exists=lambda doctype, name: (doctype, name) in self.rows,
		)
		self.add("BOQ", "BASE", status="Approved", revision=1)
		self.add("BOQ", "B", status="Draft", revision=2, base_revision="BASE")
		self.add(
			"BOQ Item",
			"A",
			boq="B",
			boq_group="G1",
			rate="10.00",
			planned_qty="2.000",
			planned_amount="20.00",
		)
		self.add(
			"BOQ Item", "Z", boq="B", boq_group="G2", rate="5.00", planned_qty="3.000", planned_amount="15.00"
		)
		self.add("Company", "CO", default_currency="USD")

	def add(self, doctype, name, **values):
		self.rows[doctype, name] = {
			**dict(
				doctype=doctype,
				name=name,
				owner=self.session.user,
				modified="2026-10-28 12:00:00",
				company="CO",
				project="P",
				docstatus=0,
				base_revision=None,
				assembly=None,
				code=name,
				description="Concrete",
				margin_rate="10",
				tax_rate="5",
			),
			**values,
		}

	@staticmethod
	def matches(row, filters):
		for key, value in (filters or {}).items():
			actual = row.get(key)
			if isinstance(value, (list, tuple)):
				op, expected = value
				if op == "in" and actual not in expected:
					return False
				if op == "!=" and actual == expected:
					return False
				if op == "like" and expected.strip("%") not in str(actual):
					return False
			elif actual != value:
				return False
		return True

	def get_doc(self, doctype, name, *, for_update=False):
		if for_update:
			self.events.append(("lock", doctype, name))
			if self.on_lock:
				self.on_lock(doctype, name)
		return LineDoc(_store=self, **copy.deepcopy(self.rows[doctype, name]))

	def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		rows = (
			self.comments
			if doctype == "Comment"
			else [v for (dt, _), v in self.rows.items() if dt == doctype]
		)
		rows = [row for row in rows if self.matches(row, filters)]
		if pluck:
			return [row.get(pluck) for row in rows]
		return [LineDoc(_store=self, **copy.deepcopy(row)) for row in rows]

	get_list = get_all

	def get_value(self, doctype, name, fieldname, **kwargs):
		if isinstance(name, dict):
			rows = self.get_all(doctype, filters=name)
			return rows[0].get(fieldname) if rows else None
		return self.rows.get((doctype, name), {}).get(fieldname)

	def count(self, doctype, filters=None):
		return len(self.get_all(doctype, filters=filters))

	def has_permission(self, doctype, ptype, doc=None, **kwargs):
		return (doctype, getattr(doc, "name", doc), ptype) not in self.denied

	@staticmethod
	def throw(message, exc=ValueError):
		raise exc(message)

	@staticmethod
	def parse_json(value):
		return json.loads(value)

	def savepoint(self, name):
		self.events.append(("savepoint", name))
		self.snapshot = copy.deepcopy((self.rows, self.comments, self.versions))

	def rollback(self, *, save_point):
		self.events.append(("rollback", save_point))
		self.rows, self.comments, self.versions = copy.deepcopy(self.snapshot)

	def recompute(self, name):
		assert not self.flags.get("boq_skip_rollup")
		self.events.append(("rollup", name))
		row = self.rows["BOQ", name]
		planned = sum(
			Decimal(v["planned_amount"])
			for (dt, _), v in self.rows.items()
			if dt == "BOQ Item" and v["boq"] == name
		)
		row["planned_amount"] = str(planned)
		margin = planned * Decimal(row["margin_rate"]) / 100
		tax = (planned + margin) * Decimal(row["tax_rate"]) / 100
		row.update(margin_amount=str(margin), tax_amount=str(tax), total=str(planned + margin + tax))
		row["modified"] = "2026-10-28 12:01:00"


class TestBOQLineValues(unittest.TestCase):
	def setUp(self):
		self.store = LineStore()
		self.addCleanup(patch.stopall)
		patch.object(api, "frappe", self.store).start()
		patch.object(api, "recompute_boq", self.store.recompute).start()
		# Native signing is a framework boundary; this site-local key is test data.
		signer = ModuleType("frappe.utils.verified_command")
		signer.get_signed_params = lambda params: (
			params
			+ "&_signature="
			+ hmac.new(b"disposable-test-key", params.encode(), hashlib.sha512).hexdigest()
		)
		patch.dict(sys.modules, {"frappe.utils.verified_command": signer}).start()

	def call(self, method, *args, **kwargs):
		function = getattr(api, method, None)
		self.assertTrue(callable(function), f"Missing behavior: {method} is not implemented")
		return function(*args, **kwargs)

	def preview(self, changes=None, request_id="req-1"):
		return self.call(
			"preview_update_line_values",
			"B",
			changes or [{"boq_item": "A", "rate": "12.00"}],
			request_id=request_id,
		)

	def commit(self, changes, preview=None, **kwargs):
		preview = preview or self.preview(changes)
		return self.call(
			"update_line_values", "B", changes, preview["fingerprint"], "req-1", test_run=0, **kwargs
		)

	def unchanged(self, before):
		self.assertEqual(self.store.rows, before)
		self.assertEqual(self.store.comments, [])
		self.assertEqual(self.store.versions, [])

	def receipt(self, fingerprint):
		return {
			"boq": "B",
			"currency": "USD",
			"owner": "qs@example.test",
			"owner_ack_required": False,
			"fingerprint": fingerprint,
			"status": "COMMITTED",
			"request_id": "req-1",
			"line_count": 1,
			"actor": "qs@example.test",
			"edit_other_draft": 0,
		}

	def legacy_receipt(self, changes, preview):
		# Historical v1 receipts were signed with totals; extra nested financial
		# fields also exercise deny-by-default projection, not a totals blacklist.
		self.commit(changes, preview)
		payload = {
			"contract": "BOQLineValues.v1",
			"request_id": "req-1",
			"outcome": {
				**self.receipt(preview["fingerprint"]),
				"totals": copy.deepcopy(preview["totals"]),
				"lines": copy.deepcopy(preview["lines"]),
				"groups": copy.deepcopy(preview["groups"]),
				"historical_values": {"rate": "10.00", "delta": "4.62"},
			},
		}
		payload["signature"] = api._line_outcome_signature(payload)
		self.store.comments[0]["content"] = "<pre>" + html.escape(json.dumps(payload)) + "</pre>"

	def test_preview_returns_precision_currency_deltas_totals_and_writes_nothing(self):
		before = copy.deepcopy(self.store.rows)
		result = self.preview()
		self.assertEqual(result["currency"], "USD")
		self.assertEqual(
			result["lines"][0]["before"],
			{"rate": "10.00", "planned_qty": "2.000", "planned_amount": "20.00", "assembly": None},
		)
		self.assertEqual(result["lines"][0]["after"]["planned_amount"], "24.00")
		self.assertEqual(result["lines"][0]["delta"], "4.00")
		self.assertEqual(
			result["groups"][0], {"boq_group": "G1", "before": "20.00", "after": "24.00", "delta": "4.00"}
		)
		self.assertEqual(result["totals"]["before"]["total"], "40.43")
		self.assertEqual(result["totals"]["after"]["total"], "45.05")
		self.assertEqual(result["totals"]["delta"], "4.62")
		self.unchanged(before)
		self.assertFalse(any(e[0] in ("savepoint", "rollup", "save") for e in self.store.events))
		self.call(
			"update_line_values", "B", [{"boq_item": "A", "rate": "12"}], result["fingerprint"], "req-1"
		)
		self.unchanged(before)

	def test_commit_refuses_entire_batch_on_one_bad_line(self):
		self.preview()  # The RED is missing executor behavior, not an exception mismatch.
		for changes, reason in [
			([{"boq_item": "A", "rate": "12"}, {"boq_item": "Z", "owner": "attacker"}], "BOQ_LINES_INVALID"),
			([{"boq_item": "A", "rate": "12", "planned_qty": "3"}], "BOQ_LINES_INVALID"),
			([{"boq_item": "A", "rate": "12"}] * 2, "BOQ_LINES_INVALID"),
			([{"boq_item": f"I-{i}", "rate": "1"} for i in range(1001)], "BOQ_LINES_TOO_MANY"),
		]:
			with self.subTest(reason=reason, size=len(changes)):
				before = copy.deepcopy(self.store.rows)
				with self.assertRaisesRegex(ValueError, reason):
					self.commit(changes)
				self.unchanged(before)
		changes = [{"boq_item": "A", "rate": "12"}, {"boq_item": "Z", "planned_qty": "4"}]
		preview = self.preview(changes)
		for failure in ("fail_save", "reset_save", "fail_comment"):
			with self.subTest(failure=failure):
				before = copy.deepcopy(self.store.rows)
				setattr(self.store, failure, True if failure == "fail_comment" else "Z")
				with self.assertRaises(ValueError):
					self.commit(changes, preview)
				self.unchanged(before)
				self.assertIsNone(self.store.flags.get("boq_skip_rollup"))
				setattr(self.store, failure, False if failure == "fail_comment" else None)
		self.store.rows["BOQ Item", "Z"]["boq"] = "OTHER"
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_NOT_IN_BOQ"):
			self.commit(changes, preview)

	def test_permlevel_without_write_refused_not_reset(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.store.denied_fields.add(("A", "rate"))
		before = copy.deepcopy(self.store.rows)
		result = self.preview(changes)
		self.assertEqual(result["lines"][0]["reason"], "BOQ_LINE_FIELD_NOT_PERMITTED")
		with self.assertRaisesRegex(PermissionError, "BOQ_LINE_FIELD_NOT_PERMITTED"):
			self.commit(changes, preview)
		self.unchanged(before)
		self.store.denied_fields.clear()
		for denied in (("BOQ", "B", "write"), ("BOQ Item", "A", "write")):
			self.store.denied.add(denied)
			with self.assertRaises(PermissionError):
				self.commit(changes, preview)
			self.unchanged(before)
			self.store.denied.clear()

	def test_owner_draft_requires_explicit_consent(self):
		self.store.rows["BOQ", "B"]["owner"] = "other@example.test"
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.assertTrue(preview["owner_ack_required"])
		with self.assertRaisesRegex(ValueError, "BOQ_DRAFT_OWNER_ACK_REQUIRED"):
			self.commit(changes, preview)
		outcome = self.commit(changes, preview, edit_other_draft=1)
		self.assertEqual(outcome["owner"], "other@example.test")
		self.assertEqual(outcome["actor"], "qs@example.test")
		self.assertEqual(outcome["edit_other_draft"], 1)
		self.assertEqual(len(self.store.comments), 1)

	def test_non_current_or_ambiguous_revision_refused(self):
		self.preview()
		original = copy.deepcopy(self.store.rows)
		for status in ("Submitted", "Approved", "Superseded"):
			self.store.rows["BOQ", "B"]["status"] = status
			with self.assertRaisesRegex(ValueError, "BOQ_NOT_CURRENT_REVISION"):
				self.preview()
		self.store.rows = copy.deepcopy(original)
		self.store.rows["BOQ", "BASE"]["status"] = "Superseded"
		with self.assertRaisesRegex(ValueError, "BOQ_NOT_CURRENT_REVISION"):
			self.preview()
		self.store.rows = copy.deepcopy(original)
		for status in ("Draft", "Submitted"):
			self.store.add("BOQ", "OTHER", status=status, base_revision="BASE", revision=3)
			with self.assertRaisesRegex(ValueError, "BOQ_NOT_CURRENT_REVISION"):
				self.preview()
		self.store.rows = copy.deepcopy(original)
		self.store.rows["BOQ", "B"]["base_revision"] = None
		with self.assertRaisesRegex(ValueError, "BOQ_NOT_CURRENT_REVISION"):
			self.preview()  # An unscoped draft cannot supersede an Approved bill.

	def test_rate_on_buildup_line_refused(self):
		self.store.add("BOQ Sub Item", "SUB", boq="B", boq_item="A")
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["reason"], "BOQ_LINE_RATE_FROM_BUILDUP")
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_RATE_FROM_BUILDUP"):
			self.commit(changes, preview)
		self.assertEqual(self.preview([{"boq_item": "A", "planned_qty": "4"}])["lines"][0]["status"], "OK")
		changes = [{"boq_item": "A", "rate": "12", "assembly": "ASM"}]
		self.store.add("Assembly", "ASM")
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["reason"], "BOQ_LINE_RATE_FROM_BUILDUP")
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_RATE_FROM_BUILDUP"):
			self.commit(changes, preview)
		del self.store.rows["BOQ Sub Item", "SUB"]
		preview = self.preview(changes)
		self.commit(changes, preview)
		self.assertEqual(self.store.rows["BOQ Item", "A"]["assembly"], "ASM")

	def test_direct_value_precision_refused_percent_rounds_half_up(self):
		self.store.rows["BOQ Item", "A"].update(rate="1.00", planned_qty="2.000", planned_amount="2.00")
		result = self.preview([{"boq_item": "A", "rate_change_percent": "0.5"}])
		self.assertEqual(result["lines"][0]["after"]["rate"], "1.01")
		for field, value in (
			("rate", "1.001"),
			("planned_qty", "1.0001"),
			("rate", "NaN"),
			("rate", "Infinity"),
			("rate", "-1"),
		):
			result = self.preview([{"boq_item": "A", field: value}])
			self.assertEqual(result["lines"][0]["status"], "REFUSED")
		self.store.precisions["rate"] = 0
		self.assertEqual(
			self.preview([{"boq_item": "A", "rate_change_percent": "50"}])["lines"][0]["after"]["rate"], "2"
		)

	def test_stale_fingerprint_refused_under_lock(self):
		changes = [{"boq_item": "Z", "rate": "6"}, {"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.store.events.clear()

		def concurrent_edit(doctype, name):
			if (doctype, name) == ("BOQ Item", "Z"):
				self.store.rows[doctype, name]["modified"] = "2026-10-28 12:00:01"

		self.store.on_lock = concurrent_edit
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_STALE"):
			self.commit(changes, preview)
		self.assertEqual(
			[e for e in self.store.events if e[0] == "lock"][:3],
			[("lock", "BOQ", "B"), ("lock", "BOQ Item", "A"), ("lock", "BOQ Item", "Z")],
		)
		self.assertFalse(any(e[0] == "save" for e in self.store.events))
		self.store.on_lock = None
		preview = self.preview(changes)
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_STALE"):
			self.commit([{"boq_item": "A", "rate": "13"}], preview)

	def test_one_rollup_and_versions_for_changed_lines(self):
		changes = [{"boq_item": "Z", "rate": "6"}, {"boq_item": "A", "rate": "12"}]
		result = self.commit(changes)
		self.assertEqual(result["status"], "COMMITTED")
		self.assertEqual(result["line_count"], 2)
		self.assertEqual(self.store.versions, ["A", "Z"])
		self.assertEqual([e for e in self.store.events if e[0] == "rollup"], [("rollup", "B")])
		self.assertTrue(all(e[3] is True for e in self.store.events if e[0] == "save"))
		self.assertEqual(Decimal(self.store.rows["BOQ", "B"]["planned_amount"]), Decimal("42"))
		self.assertEqual(len(self.store.comments), 1)

	def test_same_request_id_returns_committed_outcome_without_second_write(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		result = self.commit(changes, preview)
		self.store.rows["BOQ", "B"]["status"] = "Approved"
		self.assertEqual(self.commit(changes, preview), result)
		self.assertEqual(self.call("get_line_values_outcome", "B", "req-1"), result)
		self.assertEqual(len(self.store.comments), 1)
		self.assertEqual(self.store.versions, ["A"])
		self.store.denied.add(("BOQ", "B", "read"))
		with self.assertRaises(PermissionError):
			self.call("get_line_values_outcome", "B", "req-1")

	def test_unknown_job_maps_to_safe_retry_or_stale_not_success(self):
		preview = self.preview()
		outcome = self.call("get_line_values_outcome", "B", "req-1", preview["fingerprint"])
		self.assertEqual(outcome["status"], "NOT_APPLIED_SAFE_RETRY")
		self.store.rows["BOQ Item", "A"]["modified"] = "2026-10-28 12:00:01"
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "req-1", preview["fingerprint"])["status"],
			"STALE_REPREVIEW",
		)
		self.assertEqual(self.call("get_line_values_outcome", "B", "req-1")["status"], "STALE_REPREVIEW")

	def test_boq_item_track_changes_enabled(self):
		meta = json.loads(
			(
				Path(__file__).resolve().parents[1] / "buildsuite_core/doctype/boq_item/boq_item.json"
			).read_text()
		)
		self.assertEqual(meta.get("track_changes"), 1, "Native line Version history is not enabled")
		self.assertGreater(
			meta["modified"], "2026-10-17 12:00:00.000000", "migrate must import the metadata change"
		)

	def test_replay_rechecks_line_permissions_and_binds_canonical_changes(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.commit(changes, preview)
		self.store.denied.add(("BOQ Item", "A", "write"))
		with self.assertRaises(PermissionError):
			self.commit(changes, preview)
		self.store.denied.clear()
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_STALE"):
			self.commit([{"boq_item": "A", "rate": "999"}], preview)

	def test_unsigned_or_edited_comment_is_not_commit_evidence(self):
		preview = self.preview()
		self.store.comments.append(
			{
				"comment_type": "Comment",
				"reference_doctype": "BOQ",
				"reference_name": "B",
				"content": json.dumps(
					{
						"contract": "BOQLineValues.v1",
						"request_id": "req-1",
						"outcome": {"status": "COMMITTED", "boq": "B", "fingerprint": preview["fingerprint"]},
					}
				),
			}
		)
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "req-1", preview["fingerprint"])["status"],
			"NOT_APPLIED_SAFE_RETRY",
		)
		self.store.comments.clear()
		changes = [{"boq_item": "A", "rate": "12"}]
		self.commit(changes, preview)
		self.store.comments[0]["content"] = self.store.comments[0]["content"].replace(
			"qs@example.test", "forged@example.test"
		)
		self.assertNotEqual(
			self.call("get_line_values_outcome", "B", "req-1", preview["fingerprint"])["status"], "COMMITTED"
		)

	def test_saved_boq_totals_mismatch_rolls_back_the_batch(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		before = copy.deepcopy(self.store.rows)

		def wrong_rollup(name):
			self.store.recompute(name)
			self.store.rows["BOQ", name]["planned_amount"] = "999.00"

		with patch.object(api, "recompute_boq", wrong_rollup):
			with self.assertRaisesRegex(ValueError, "BOQ_LINE_SAVED_VALUES_MISMATCH"):
				self.commit(changes, preview)
		self.unchanged(before)

	def test_preview_does_not_disclose_a_read_restricted_line_field(self):
		self.store.denied_read_fields.add(("Z", "rate"))
		with self.assertRaisesRegex(PermissionError, "BOQ_LINE_FIELD_NOT_PERMITTED"):
			self.preview()

	def test_durable_comment_keeps_summary_not_restricted_line_values(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		result = self.commit(changes, preview)
		payload = json.loads(html.unescape(self.store.comments[0]["content"][5:-6]))
		signature = payload.pop("signature")
		self.assertEqual(signature, api._line_outcome_signature(payload))
		self.assertEqual(set(payload), {"contract", "request_id", "outcome"})
		self.assertNotIn("totals", payload["outcome"])
		self.assertEqual(payload["outcome"], self.receipt(preview["fingerprint"]))
		self.assertEqual(result, self.receipt(preview["fingerprint"]))
		self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}\.[0-9a-f]{64}$")
		self.assertNotIn("Concrete", self.store.comments[0]["content"])

	def test_restricted_boq_reader_gets_committed_metadata_without_financial_access(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.commit(changes, preview)
		self.store.session.user = "reader@example.test"
		self.store.denied.add(("BOQ", "B", "write"))
		self.store.denied.add(("BOQ Item", "A", "read"))
		self.store.denied_read_fields.update(
			(name, field) for name in ("A", "Z") for field in ("rate", "planned_qty", "planned_amount")
		)
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "req-1"), self.receipt(preview["fingerprint"])
		)
		self.assertEqual(self.store.versions, ["A"])

	def test_metadata_only_replay_keeps_one_native_write_and_one_comment(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.commit(changes, preview)
		self.store.rows["BOQ", "B"]["status"] = "Approved"
		self.assertEqual(self.commit(changes, preview), self.receipt(preview["fingerprint"]))
		self.assertEqual(len(self.store.comments), 1)
		self.assertEqual(self.store.versions, ["A"])
		self.assertEqual(len([e for e in self.store.events if e[0] == "save"]), 1)
		self.assertEqual(len([e for e in self.store.events if e[0] == "rollup"]), 1)

	def test_legacy_signed_receipt_read_sanitizes_even_when_current_header_is_readable(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.legacy_receipt(changes, preview)
		comments = copy.deepcopy(self.store.comments)
		# Current header visibility never authorizes historical line amounts.
		self.store.rows["BOQ", "B"]["total"] = "1234.00"
		self.store.session.user = "reader@example.test"
		self.store.denied_read_fields.add(("A", "planned_amount"))
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "req-1"), self.receipt(preview["fingerprint"])
		)
		self.assertEqual(self.store.comments, comments, "Legacy Comments must not be rewritten")

	def test_legacy_signed_receipt_replay_sanitizes_without_second_write(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.legacy_receipt(changes, preview)
		comments = copy.deepcopy(self.store.comments)
		self.assertEqual(self.commit(changes, preview), self.receipt(preview["fingerprint"]))
		self.assertEqual(self.store.comments, comments)
		self.assertEqual(self.store.versions, ["A"])

	def test_receipt_recovery_requires_authenticated_caller_even_with_boq_read(self):
		self.commit([{"boq_item": "A", "rate": "12"}])
		for user in ("Guest", "", None):
			with self.subTest(user=user):
				self.store.session.user = user
				self.store.events.clear()
				with self.assertRaisesRegex(PermissionError, "BOQ_LINE_FIELD_NOT_PERMITTED"):
					self.call("get_line_values_outcome", "B", "req-1")
				self.assertEqual(self.store.events, [])

	def test_receipt_is_bound_to_site_boq_request_and_current_read_permission(self):
		self.commit([{"boq_item": "A", "rate": "12"}])
		self.assertEqual(self.call("get_line_values_outcome", "BASE", "req-1")["status"], "STALE_REPREVIEW")
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "other-request")["status"], "STALE_REPREVIEW"
		)
		self.store.denied.add(("BOQ", "B", "read"))
		with self.assertRaises(PermissionError):
			self.call("get_line_values_outcome", "B", "req-1")
		self.store.denied.clear()
		with patch(
			"frappe.utils.verified_command.get_signed_params",
			side_effect=lambda params: (
				params
				+ "&_signature="
				+ hmac.new(b"other-site-test-key", params.encode(), hashlib.sha512).hexdigest()
			),
		):
			self.assertEqual(self.call("get_line_values_outcome", "B", "req-1")["status"], "STALE_REPREVIEW")

	def test_quantity_edit_preserves_unrequested_snapshot_rate_precision(self):
		# PR #13's production snapshots can carry more digits than display precision.
		self.store.rows["BOQ Item", "A"].update(rate="10.005", planned_amount="20.010")
		changes = [{"boq_item": "A", "planned_qty": "3"}]
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["before"]["rate"], "10.005")
		self.assertEqual(preview["lines"][0]["after"]["rate"], "10.005")
		self.assertEqual(preview["lines"][0]["after"]["planned_amount"], "30.02")
		self.commit(changes, preview)
		self.assertEqual(self.store.rows["BOQ Item", "A"]["rate"], "10.005")

	def test_commit_is_only_exposed_over_post_not_a_rolled_back_get(self):
		# Frappe rolls GET requests back: returning COMMITTED over GET is false.
		methods = getattr(api.update_line_values, "_http_methods", None)
		if methods is None:
			import frappe

			methods = frappe.allowed_http_methods_for_whitelisted_func[api.update_line_values]
		self.assertEqual(methods, ["POST"])

	def test_assembly_must_exist_be_enabled_and_be_readable(self):
		changes = [{"boq_item": "A", "rate": "12", "assembly": "ASM"}]
		for state, reason in (
			("missing", "BOQ_LINE_ASSEMBLY_NOT_FOUND"),
			("disabled", "BOQ_LINE_ASSEMBLY_DISABLED"),
			("unreadable", "BOQ_LINE_FIELD_NOT_PERMITTED"),
		):
			with self.subTest(state=state):
				if state != "missing":
					self.store.add("Assembly", "ASM", disabled=int(state == "disabled"))
				if state == "unreadable":
					self.store.denied.add(("Assembly", "ASM", "read"))
				before = copy.deepcopy(self.store.rows)
				preview = self.preview(changes)
				self.assertEqual(preview["lines"][0]["reason"], reason)
				with self.assertRaisesRegex(PermissionError if state == "unreadable" else ValueError, reason):
					self.commit(changes, preview)
				self.unchanged(before)

	def test_assembly_apply_refuses_existing_assembly_without_sub_items(self):
		self.store.add("Assembly", "ASM", disabled=0)
		self.store.rows["BOQ Item", "A"]["assembly"] = "OLD"
		changes = [{"boq_item": "A", "rate": "12", "assembly": "ASM"}]
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["reason"], "BOQ_LINE_RATE_FROM_BUILDUP")
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_RATE_FROM_BUILDUP"):
			self.commit(changes, preview)

	def test_native_float_half_cent_preview_matches_commit(self):
		self.store.rows["BOQ Item", "A"].update(planned_qty="878.500", planned_amount="8785.000")
		changes = [{"boq_item": "A", "rate": "8909.13"}]
		preview = self.preview(changes)
		self.assertEqual(preview["lines"][0]["after"]["planned_amount"], "7826670.70")
		self.assertEqual(self.commit(changes, preview)["status"], "COMMITTED")
		self.assertEqual(self.store.rows["BOQ Item", "A"]["planned_amount"], "7826670.704999999")

	def test_write_permission_and_membership_refusals_take_no_locks(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		for denied, reason in (
			(("BOQ", "B", "write"), "BOQ_WRITE_NOT_PERMITTED"),
			(("BOQ Item", "A", "write"), "BOQ_LINE_FIELD_NOT_PERMITTED"),
		):
			with self.subTest(denied=denied):
				self.store.denied = {denied}
				self.store.events.clear()
				with self.assertRaisesRegex(PermissionError, reason):
					self.commit(changes, preview)
				self.assertEqual(self.store.events, [])
		self.store.denied.clear()

	def test_field_permission_refusal_takes_no_locks(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		self.store.denied_fields.add(("A", "rate"))
		with self.assertRaisesRegex(PermissionError, "BOQ_LINE_FIELD_NOT_PERMITTED"):
			self.commit(changes, preview)
		self.assertEqual(self.store.events, [])

	def test_missing_or_foreign_line_refusal_takes_no_locks(self):
		preview = self.preview()
		for name in ("FOREIGN", "MISSING"):
			with self.subTest(name=name):
				self.store.add("BOQ Item", "FOREIGN", boq="OTHER")
				self.store.events.clear()
				with self.assertRaisesRegex(ValueError, "BOQ_LINE_NOT_IN_BOQ"):
					self.commit([{"boq_item": name, "rate": "12"}], preview)
				self.assertEqual(self.store.events, [])

	def test_outcome_read_permission_and_maintenance_reads_take_no_locks(self):
		self.preview()
		self.store.denied.add(("BOQ", "B", "read"))
		with self.assertRaises(PermissionError):
			self.call("get_line_values_outcome", "B", "req-1")
		self.assertEqual(self.store.events, [])

	def test_maintenance_outcome_read_takes_no_locks(self):
		preview = self.preview()
		self.store.flags.read_only = True

		def read_only(doctype, name):
			raise ValueError("InReadOnlyMode")

		self.store.on_lock = read_only
		self.assertEqual(
			self.call("get_line_values_outcome", "B", "req-1", preview["fingerprint"])["status"],
			"NOT_APPLIED_SAFE_RETRY",
		)
		self.assertEqual(self.store.events, [])

	def test_rollup_suspended_refuses_without_mutation(self):
		changes = [{"boq_item": "A", "rate": "12"}]
		preview = self.preview(changes)
		before = copy.deepcopy(self.store.rows)
		self.store.flags.boq_skip_rollup = True
		with self.assertRaisesRegex(ValueError, "BOQ_LINES_ROLLUP_SUSPENDED"):
			self.commit(changes, preview)
		self.unchanged(before)
		self.assertTrue(self.store.flags.boq_skip_rollup)

	def test_missing_currency_and_precision_fail_closed(self):
		self.store.rows["Company", "CO"]["default_currency"] = None
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_CURRENCY_UNAVAILABLE"):
			self.preview()
		self.store.rows["Company", "CO"]["default_currency"] = "USD"
		self.store.precisions["rate"] = None
		with self.assertRaisesRegex(ValueError, "BOQ_LINE_PRECISION_UNAVAILABLE"):
			self.preview()
		self.assertEqual(self.store.events, [])

	def test_decimal_column_value_cap_refuses_direct_and_computed_values(self):
		for field, value in (
			("rate", "1000000000000"),
			("planned_qty", "1e12"),
			("rate_change_percent", "1e13"),
		):
			with self.subTest(field=field):
				changes = [{"boq_item": "A", field: value}]
				preview = self.preview(changes)
				self.assertEqual(preview["lines"][0]["reason"], "BOQ_LINE_VALUE_INVALID")
				with self.assertRaisesRegex(ValueError, "BOQ_LINE_VALUE_INVALID"):
					self.commit(changes, preview)
		self.assertEqual(self.store.versions, [])


if __name__ == "__main__":
	unittest.main()
