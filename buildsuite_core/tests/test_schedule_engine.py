# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""Pure-function tests for the scheduling engine (conflict flagging + cascade).

These are the SHARED TEST VECTORS: the frontend `useScheduleEngine` port must
produce identical results for the same inputs, so the client preview and the
server's authoritative commit never disagree.
"""

from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from buildsuite_core.api import schedule_engine as eng
from buildsuite_core.tests.base import BuildSuiteTestCase


def _t(name, type="Activity", start=None, end=None, status="Open", act_start_date=None):
	return {
		"name": name,
		"type": type,
		"start": start,
		"end": end,
		"status": status,
		"act_start_date": act_start_date,
	}


def _e(pred, succ, type="FS", lag=0):
	return {"predecessor": pred, "successor": succ, "type": type, "lag": lag}


class TestScheduleEngine(UnitTestCase):
	def test_cascade_moves_only_native_reschedulable_statuses(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"open": _t("open", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Open"),
			"working": _t("working", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Working"),
			"overdue": _t("overdue", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Overdue"),
			"completed": _t("completed", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Completed"),
			"cancelled": _t("cancelled", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Cancelled"),
			"template": _t("template", start=date(2026, 1, 1), end=date(2026, 1, 5), status="Template"),
		}
		moves = eng.compute_cascade(
			"A",
			tasks,
			[_e("A", name) for name in tasks if name != "A"],
			{"start": date(2026, 1, 1), "end": date(2026, 1, 20)},
		)
		self.assertEqual(
			{move["task"] for move in moves},
			{"open", "working", "overdue"},
		)
		self.assertTrue(all(eng._is_reschedulable_status(tasks[move["task"]]["status"]) for move in moves))

	def test_cascade_ss_uses_start_only_predecessor(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=None),
			"B": _t("B", start=date(2026, 1, 2), end=date(2026, 1, 6)),
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B", type="SS", lag=5)],
			{"start": date(2026, 1, 10), "end": None},
		)
		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["new_start"], "2026-01-15")
		self.assertEqual(moves[0]["new_end"], "2026-01-19")

	def test_negative_fs_lag_allows_successor_before_predecessor_end(self):
		pred = _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10))
		succ = _t("B", start=date(2026, 1, 8), end=date(2026, 1, 18))
		self.assertEqual(
			eng._earliest_start_for_edge(pred, succ, _e("A", "B", type="FS", lag=-2)),
			date(2026, 1, 8),
		)

	def test_ss_uses_predecessor_start_and_ignores_end_only_change(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 6), end=date(2026, 1, 16)),
		}
		edges = [_e("A", "B", type="SS", lag=5)]
		self.assertEqual(eng._compute_earliest_start("B", tasks, edges)[0], date(2026, 1, 6))
		self.assertEqual(
			eng.compute_cascade(
				"A", tasks, edges, {"start": date(2026, 1, 1), "end": date(2026, 1, 20)}
			),
			[],
		)

	def test_ff_tracks_predecessor_end_and_preserves_duration(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 20)),
			"B": _t("B", start=date(2026, 1, 3), end=date(2026, 1, 8)),
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B", type="FF", lag=2)],
			{"start": date(2026, 1, 1), "end": date(2026, 1, 25)},
		)
		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["new_start"], "2026-01-22")
		self.assertEqual(moves[0]["new_end"], "2026-01-27")

	def test_five_day_slip_moves_relationship_successors_but_not_summary_tasks(self):
		tasks = {
			"2.1": _t("2.1", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"1.0": _t("1.0", type="Activity", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"2.0": _t("2.0", type="Activity", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"3.0": _t("3.0", type="Activity", start=date(2026, 1, 1), end=date(2026, 1, 10)),
		}
		for task_id in ("2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4", "4.1", "4.2", "4.3", "4.4"):
			tasks[task_id] = _t(task_id, start=date(2026, 1, 10), end=date(2026, 1, 12))
		edges = [_e("2.1", task_id) for task_id in tasks if task_id not in {"2.1", "1.0", "2.0", "3.0"}]
		moves = eng.compute_cascade(
			"2.1", tasks, edges, {"start": date(2026, 1, 6), "end": date(2026, 1, 15)}
		)
		self.assertEqual({m["task"] for m in moves}, set(tasks) - {"2.1", "1.0", "2.0", "3.0"})
		self.assertEqual(len(moves), 11)
		self.assertIn("2.2", {m["task"] for m in moves})
		self.assertIn("3.1", {m["task"] for m in moves})
		self.assertTrue(all(m["new_start"] == "2026-01-15" for m in moves))

	def test_load_graph_drops_automatic_child_to_parent_edge(self):
		class Row:
			def __init__(self, **values):
				self.__dict__.update(values)

		def get_all(doctype, **kwargs):
			if doctype == "Task":
				return [
					Row(name="1.0", type="Activity", exp_start_date="2026-01-01", exp_end_date="2026-01-10", status="Open", parent_task=None),
					Row(name="1.3", type="Activity", exp_start_date="2026-01-02", exp_end_date="2026-01-09", status="Open", parent_task="1.0"),
					Row(name="2.1", type="Activity", exp_start_date="2026-01-01", exp_end_date="2026-01-10", status="Open", parent_task=None),
					Row(name="2.2", type="Activity", exp_start_date="2026-01-10", exp_end_date="2026-01-12", status="Open", parent_task=None),
				]
			return [
				Row(parent="1.0", task="1.3", dependency_type="FS", lag_days=0),
				Row(parent="2.2", task="2.1", dependency_type="FS", lag_days=0),
			]

		with patch.object(eng.frappe, "get_all", side_effect=get_all):
			_, edges = eng._load_graph("PROJ-1")
		self.assertEqual(edges, [{"predecessor": "2.1", "successor": "2.2", "type": "FS", "lag": 0}])

	def test_fs_conflict_flagged(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 5), end=date(2026, 1, 15)),  # starts before A ends
		}
		res = eng.compute_conflicts("A", tasks, [_e("A", "B")])
		self.assertTrue(res["B"][0])
		self.assertIn("earlier than allowed", res["B"][1])
		self.assertFalse(res["A"][0])

	def test_fs_satisfied_no_conflict(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 11), end=date(2026, 1, 20)),
		}
		res = eng.compute_conflicts("A", tasks, [_e("A", "B")])
		self.assertFalse(res["B"][0])

	def test_cascade_shifts_duration_preserving(self):
		# B (10-day span) FS-depends on A. FS earliest = pred end + lag (same-day start
		# allowed, per the locked prototype convention). Move A to end 01-20 -> B
		# starts 01-20, ends 01-30.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 11), end=date(2026, 1, 21)),
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B")], {"start": date(2026, 1, 11), "end": date(2026, 1, 20)}
		)
		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["task"], "B")
		self.assertEqual(moves[0]["new_start"], "2026-01-20")
		self.assertEqual(moves[0]["new_end"], "2026-01-30")
		self.assertIs(moves[0]["is_milestone"], False)

	def test_cascade_no_pull_earlier(self):
		# B already far later than A's end -> cascade leaves it untouched.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 2, 1), end=date(2026, 2, 10)),
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B")], {"start": date(2026, 1, 3), "end": date(2026, 1, 12)}
		)
		self.assertEqual(moves, [])

	def test_milestone_successor_single_date(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"M": _t("M", type="Milestone", start=None, end=date(2026, 1, 5)),  # due before A ends
		}
		edges = [_e("A", "M")]
		res = eng.compute_conflicts("A", tasks, edges)
		self.assertTrue(res["M"][0])
		self.assertIn("Due", res["M"][1])
		moves = eng.compute_cascade("A", tasks, edges)
		self.assertEqual(len(moves), 1)
		self.assertTrue(moves[0]["is_milestone"])
		self.assertEqual(moves[0]["new_end"], "2026-01-10")
		self.assertIsNone(moves[0]["new_start"])

	def test_asap_fs_minus_two_pulls_successor_from_predecessor_or_successor_root(self):
		tasks = {
			"pred": _t("pred", start=date(2026, 3, 20), end=date(2026, 3, 31)),
			"succ": _t("succ", start=date(2026, 4, 1), end=date(2026, 4, 10)),
		}
		edges = [_e("pred", "succ", type="FS", lag=-2)]

		from_predecessor = eng.compute_cascade("pred", tasks, edges, asap=True)
		from_successor = eng.compute_cascade("succ", tasks, edges, asap=True)

		for moves in (from_predecessor, from_successor):
			self.assertEqual(len(moves), 1)
			self.assertEqual(moves[0]["task"], "succ")
			self.assertEqual(moves[0]["new_start"], "2026-03-29")
			self.assertEqual(moves[0]["new_end"], "2026-04-07")
			self.assertIs(moves[0]["is_milestone"], False)

	def test_asap_ss_plus_five_pulls_successor_to_predecessor_start_plus_lag(self):
		tasks = {
			"pred": _t("pred", start=date(2026, 3, 25), end=date(2026, 3, 31)),
			"succ": _t("succ", start=date(2026, 4, 2), end=date(2026, 4, 10)),
		}
		moves = eng.compute_cascade("pred", tasks, [_e("pred", "succ", type="SS", lag=5)], asap=True)

		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["new_start"], "2026-03-30")
		self.assertEqual(moves[0]["new_end"], "2026-04-07")

	def test_asap_only_pulls_open_or_overdue_without_actual_start(self):
		tasks = {
			"pred": _t("pred", start=date(2026, 3, 20), end=date(2026, 3, 31)),
			"open": _t("open", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Open"),
			"overdue": _t("overdue", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Overdue"),
			"completed": _t("completed", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Completed"),
			"working": _t("working", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Working"),
			"cancelled": _t("cancelled", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Cancelled"),
			"template": _t("template", start=date(2026, 4, 1), end=date(2026, 4, 10), status="Template"),
			"actual": _t(
				"actual",
				start=date(2026, 4, 1),
				end=date(2026, 4, 10),
				status="Open",
				act_start_date=date(2026, 3, 31),
			),
		}
		edges = [_e("pred", task_id, type="FS", lag=-2) for task_id in tasks if task_id != "pred"]

		moves = eng.compute_cascade("pred", tasks, edges, asap=True)

		self.assertEqual({move["task"] for move in moves}, {"open", "overdue"})
		self.assertTrue(all(move["is_milestone"] is False for move in moves))

	def test_asap_root_alignment_uses_pull_eligibility_for_push_and_pull(self):
		predecessor = _t("pred", start=date(2026, 3, 1), end=date(2026, 3, 20))

		for status, act_start_date in (
			("Working", None),
			("Open", date(2026, 3, 2)),
			("Overdue", date(2026, 3, 2)),
		):
			with self.subTest(status=status, act_start_date=act_start_date):
				tasks = {
					"pred": predecessor,
					"root": _t(
						"root",
						start=date(2026, 3, 1),
						end=date(2026, 3, 6),
						status=status,
						act_start_date=act_start_date,
					),
				}
				self.assertEqual(
					eng.compute_cascade("root", tasks, [_e("pred", "root")], asap=True),
					[],
				)

		for status in ("Open", "Overdue"):
			with self.subTest(status=status):
				tasks = {
					"pred": predecessor,
					"root": _t(
						"root",
						start=date(2026, 4, 10),
						end=date(2026, 4, 15),
						status=status,
					),
				}
				moves = eng.compute_cascade("root", tasks, [_e("pred", "root")], asap=True)
				self.assertEqual(len(moves), 1)
				self.assertEqual(moves[0]["task"], "root")
				self.assertEqual(moves[0]["new_start"], "2026-03-20")
				self.assertEqual(moves[0]["new_end"], "2026-03-25")

	def test_every_move_declares_boolean_milestone_flag(self):
		tasks = {
			"pred": _t("pred", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"activity": _t("activity", start=date(2026, 1, 5), end=date(2026, 1, 15)),
			"milestone": _t("milestone", type="Milestone", start=None, end=date(2026, 1, 5)),
		}
		moves = eng.compute_cascade(
			"pred",
			tasks,
			[_e("pred", "activity"), _e("pred", "milestone")],
		)

		self.assertEqual({move["task"] for move in moves}, {"activity", "milestone"})
		self.assertEqual(
			{move["task"] for move in moves if move["is_milestone"]},
			{"milestone"},
		)
		self.assertTrue(all(isinstance(move["is_milestone"], bool) for move in moves))

	def test_slip_days_moves_zero_float_and_consumes_successor_float_and_preview_matches_commit(self):
		tasks = {
			"root": _t("root", start=date(2026, 3, 1), end=date(2026, 3, 10)),
			"2.2": _t("2.2", start=date(2026, 3, 10), end=date(2026, 3, 15)),
			"3.1": _t("3.1", start=date(2026, 3, 10), end=date(2026, 3, 15)),
			"4.1": _t("4.1", start=date(2026, 3, 10), end=date(2026, 3, 15)),
			"float-3": _t("float-3", start=date(2026, 3, 13), end=date(2026, 3, 18)),
		}
		edges = [_e("root", task_id) for task_id in tasks if task_id != "root"]
		root = type(
			"Root",
			(),
			{
				"project": "PROJ-1",
				"exp_start_date": date(2026, 3, 1),
				"exp_end_date": date(2026, 3, 10),
			},
		)()

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(eng, "_load_graph", return_value=(tasks, edges)),
			patch.object(eng, "_apply_moves"),
			patch.object(eng, "recompute_schedule_conflicts"),
			patch("buildsuite_core.api.schedule_snapshot.capture_snapshot"),
		):
			preview = eng.reschedule_downstream("root", dry_run=1, slip_days=5)
			commit = eng.reschedule_downstream("root", dry_run=0, slip_days=5)

		self.assertEqual(preview, commit)
		self.assertEqual(
			{
				move["task"]: eng._diff_days(tasks[move["task"]]["start"], move["new_start"])
				for move in preview["moves"]
			},
			{"2.2": 5, "3.1": 5, "4.1": 5, "float-3": 2},
		)

	def test_load_graph_includes_actual_start_date(self):
		class Row:
			def __init__(self, **values):
				self.__dict__.update(values)

		def get_all(doctype, **kwargs):
			if doctype == "Task":
				return [
					Row(
						name="started",
						type="Activity",
						exp_start_date="2026-03-01",
						exp_end_date="2026-03-10",
						act_start_date="2026-03-02",
						status="Working",
						parent_task=None,
					)
				]
			return []

		with patch.object(eng.frappe, "get_all", side_effect=get_all):
			tasks, _ = eng._load_graph("PROJ-1")

		self.assertEqual(tasks["started"]["act_start_date"], date(2026, 3, 2))

	def test_slip_days_rejects_explicit_date_override(self):
		root = type(
			"Root",
			(),
			{
				"project": "PROJ-1",
				"exp_start_date": date(2026, 3, 1),
				"exp_end_date": date(2026, 3, 10),
			},
		)()

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(eng, "_load_graph", return_value=({"root": _t("root")}, [])),
			patch.object(eng.frappe, "throw", side_effect=ValueError),
			self.assertRaises(ValueError),
		):
			eng.reschedule_downstream(
				"root", new_start="2026-03-02", dry_run=1, slip_days=5
			)

	def test_inspection_gate_blocks_successor(self):
		tasks = {
			"I": _t("I", type="Inspection", start=date(2026, 1, 1), end=date(2026, 1, 2), status="Working"),
			"B": _t("B", start=date(2026, 1, 10), end=date(2026, 1, 20)),  # dates fine, but gate open
		}
		res = eng.compute_conflicts("I", tasks, [_e("I", "B")])
		self.assertTrue(res["B"][0])
		self.assertIn("Waiting on inspection I", res["B"][1])

	def test_completed_inspection_does_not_block(self):
		tasks = {
			"I": _t("I", type="Inspection", start=date(2026, 1, 1), end=date(2026, 1, 2), status="Completed"),
			"B": _t("B", start=date(2026, 1, 10), end=date(2026, 1, 20)),
		}
		res = eng.compute_conflicts("I", tasks, [_e("I", "B")])
		self.assertFalse(res["B"][0])

	def test_cycle_aborts_cascade(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 5)),
			"B": _t("B", start=date(2026, 1, 6), end=date(2026, 1, 10)),
		}
		self.assertIsNone(eng.compute_cascade("A", tasks, [_e("A", "B"), _e("B", "A")]))

	def test_multi_predecessor_takes_most_binding(self):
		# B depends FS on A (ends 01-10) and SS on C (starts 01-20). Most binding = 01-21.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"C": _t("C", start=date(2026, 1, 20), end=date(2026, 1, 25)),
			"B": _t("B", start=date(2026, 1, 5), end=date(2026, 1, 15)),
		}
		edges = [_e("A", "B", "FS"), _e("C", "B", "SS", 1)]
		earliest, reason = eng._compute_earliest_start("B", tasks, edges)
		self.assertEqual(earliest, date(2026, 1, 21))
		self.assertIn("from C", reason)

	def test_positive_lag_shifts_successor_later(self):
		# SCH-011 — a positive lag on an FS edge shifts the cascaded successor
		# later by exactly the lag, beyond the bare (lag=0) FS position.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 11), end=date(2026, 1, 21)),
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B", lag=3)], {"start": date(2026, 1, 1), "end": date(2026, 1, 15)}
		)
		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["new_start"], "2026-01-18")  # new A end (01-15) + lag 3
		self.assertEqual(moves[0]["new_end"], "2026-01-28")  # 10-day duration preserved

	def test_cascade_recomputes_ff_dependents(self):
		# SCH-016 — an FF-linked dependent recomputes under cascade: its END
		# tracks the predecessor's new end (+ lag), duration preserved.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 3), end=date(2026, 1, 8)),  # FF: ends before A
		}
		moves = eng.compute_cascade(
			"A", tasks, [_e("A", "B", type="FF")], {"start": date(2026, 1, 1), "end": date(2026, 1, 20)}
		)
		self.assertEqual(len(moves), 1)
		self.assertEqual(moves[0]["new_end"], "2026-01-20")  # tracks A's new end
		self.assertEqual(moves[0]["new_start"], "2026-01-15")  # 5-day duration preserved

	def test_cascade_preserves_lag_offsets(self):
		# SCH-017 — cascading through a chain (A->B->C) preserves each edge's
		# OWN lag/lead offset, not just the first hop's.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 10)),
			"B": _t("B", start=date(2026, 1, 12), end=date(2026, 1, 17)),  # FS lag +2 from A
			"C": _t("C", start=date(2026, 1, 16), end=date(2026, 1, 20)),  # FS lag -1 from B (lead)
		}
		edges = [_e("A", "B", lag=2), _e("B", "C", lag=-1)]
		moves = eng.compute_cascade("A", tasks, edges, {"start": date(2026, 1, 1), "end": date(2026, 1, 20)})
		self.assertEqual(len(moves), 2)
		b_move = next(m for m in moves if m["task"] == "B")
		c_move = next(m for m in moves if m["task"] == "C")
		self.assertEqual(b_move["new_start"], "2026-01-22")  # A's new end (01-20) + lag 2
		self.assertEqual(b_move["new_end"], "2026-01-27")  # 5-day duration preserved
		self.assertEqual(c_move["new_start"], "2026-01-26")  # B's new end (01-27) + lag -1
		self.assertEqual(c_move["new_end"], "2026-01-30")  # 4-day duration preserved

	def test_long_cycle_rejected(self):
		# SCH-019 — a longer cycle (A->B->C->A), not just a direct 2-node one,
		# is still detected and aborts the cascade.
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 5)),
			"B": _t("B", start=date(2026, 1, 6), end=date(2026, 1, 10)),
			"C": _t("C", start=date(2026, 1, 11), end=date(2026, 1, 15)),
		}
		edges = [_e("A", "B"), _e("B", "C"), _e("C", "A")]
		self.assertIsNone(eng.compute_cascade("A", tasks, edges))


class TestScheduleEngineIntegration(BuildSuiteTestCase):
	"""End-to-end over the DB: the on_update hook flags conflicts, and
	reschedule_downstream previews + commits a cascade."""

	def _task(self, project, subject, start, end, type="Activity"):
		return frappe.get_doc(
			{
				"doctype": "Task",
				"project": project,
				"subject": subject,
				"type": type,
				"exp_start_date": start,
				"exp_end_date": end,
			}
		).insert(ignore_permissions=True)

	def test_end_date_not_auto_filled_from_expected_time(self):
		# A task carrying expected_time (as template-seeded tasks do) must NOT get an
		# auto-generated end date when saved with a start but no end.
		p = self._make_project(company=self.company)
		t = frappe.get_doc(
			{
				"doctype": "Task",
				"project": p.name,
				"subject": f"ET {self._n}",
				"expected_time": 240,
				"task_status": "Yet To Start",
			}
		).insert(ignore_permissions=True)
		t.exp_start_date = "2026-02-01"
		t.save(ignore_permissions=True)
		t.reload()
		self.assertIsNone(t.exp_end_date)

	def test_milestone_type_sets_native_is_milestone(self):
		# A Milestone-type task gets ERPNext's native is_milestone flag on save, and
		# it clears again when the type changes away from Milestone.
		p = self._make_project(company=self.company)
		t = self._task(p.name, f"MS {self._n}", "2026-01-05", "2026-01-05", type="Milestone")
		self.assertEqual(t.is_milestone, 1)
		t.type = "Activity"
		t.save(ignore_permissions=True)
		self.assertEqual(t.is_milestone, 0)

	def test_hook_flags_then_cascade_clears(self):
		p = self._make_project(company=self.company)
		a = self._task(p.name, f"A {self._n}", "2026-01-01", "2026-01-10")
		b = self._task(p.name, f"B {self._n}", "2026-01-05", "2026-01-15")  # starts before A ends

		# Adding the FS edge + saving B fires the on_update recompute hook.
		b.append("depends_on", {"task": a.name, "dependency_type": "FS", "lag_days": 0})
		b.save(ignore_permissions=True)
		b.reload()
		self.assertTrue(b.schedule_conflict)
		self.assertIn("earlier than allowed", b.conflict_reason or "")

		# Dry-run cascade: moving A's end to 01-20 should propose moving B.
		preview = eng.reschedule_downstream(a.name, new_start="2026-01-01", new_end="2026-01-20", dry_run=1)
		self.assertEqual([m["task"] for m in preview["moves"]], [b.name])

		# Commit: B shifts (duration-preserving) and its conflict clears.
		eng.reschedule_downstream(a.name, new_start="2026-01-01", new_end="2026-01-20", dry_run=0)
		b.reload()
		self.assertEqual(str(b.exp_start_date)[:10], "2026-01-20")
		self.assertFalse(b.schedule_conflict)

	def test_status_only_save_does_not_reschedule_fs_or_ss_dependents(self):
		p = self._make_project(company=self.company)
		a = self._task(p.name, f"A {self._n}", "2026-01-01", "2026-01-10")
		fs = self._task(p.name, f"FS {self._n}", "2026-01-08", "2026-01-18")
		ss = self._task(p.name, f"SS {self._n}", "2026-01-06", "2026-01-16")
		fs.append("depends_on", {"task": a.name, "dependency_type": "FS", "lag_days": -2})
		fs.save(ignore_permissions=True)
		ss.append("depends_on", {"task": a.name, "dependency_type": "SS", "lag_days": 5})
		ss.save(ignore_permissions=True)

		a.status = "Working"
		a.save(ignore_permissions=True)
		fs.reload()
		ss.reload()
		self.assertEqual(str(fs.exp_start_date)[:10], "2026-01-08")
		self.assertEqual(str(fs.exp_end_date)[:10], "2026-01-18")
		self.assertEqual(str(ss.exp_start_date)[:10], "2026-01-06")
		self.assertEqual(str(ss.exp_end_date)[:10], "2026-01-16")

	def test_start_only_preview_keeps_duration_and_moves_dependents(self):
		p = self._make_project(company=self.company)
		a = self._task(p.name, f"A {self._n}", "2026-01-01", "2026-01-10")
		b = self._task(p.name, f"B {self._n}", "2026-01-11", "2026-01-21")
		b.append("depends_on", {"task": a.name, "dependency_type": "FS", "lag_days": 0})
		b.save(ignore_permissions=True)

		preview = eng.reschedule_downstream(a.name, new_start="2026-01-06", dry_run=1)
		self.assertEqual([m["task"] for m in preview["moves"]], [b.name])
		self.assertEqual(preview["moves"][0]["new_start"], "2026-01-15")
		self.assertEqual(preview["moves"][0]["new_end"], "2026-01-25")

	def test_ff_dependency_recompute(self):
		# SCH-010 — an FF (Finish-to-Finish) dependency ties the successor's END
		# to the predecessor's end (+ lag), exercised end-to-end through the
		# on_update conflict hook and a committed cascade.
		p = self._make_project(company=self.company)
		a = self._task(p.name, f"A {self._n}", "2026-01-01", "2026-01-10")
		b = self._task(p.name, f"B {self._n}", "2026-01-05", "2026-01-08")  # ends before A (FF conflict)

		b.append("depends_on", {"task": a.name, "dependency_type": "FF", "lag_days": 0})
		b.save(ignore_permissions=True)
		b.reload()
		self.assertTrue(b.schedule_conflict)

		# Move A's end later -> B's end cascades to match (duration preserved).
		eng.reschedule_downstream(a.name, new_start="2026-01-01", new_end="2026-01-20", dry_run=0)
		b.reload()
		self.assertEqual(str(b.exp_end_date)[:10], "2026-01-20")
		self.assertFalse(b.schedule_conflict)

	def test_task_move_persists_dates(self):
		# SCH-026 — moving the ROOT task itself via reschedule_downstream (not
		# just its cascaded dependents) persists its own new dates after commit.
		p = self._make_project(company=self.company)
		a = self._task(p.name, f"MOVE {self._n}", "2026-01-01", "2026-01-10")
		eng.reschedule_downstream(a.name, new_start="2026-02-01", new_end="2026-02-15", dry_run=0)
		a.reload()
		self.assertEqual(str(a.exp_start_date)[:10], "2026-02-01")
		self.assertEqual(str(a.exp_end_date)[:10], "2026-02-15")
