# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""Pure-function tests for the scheduling engine (conflict flagging + cascade).

These are the SHARED TEST VECTORS: the frontend `useScheduleEngine` port must
produce identical results for the same inputs, so the client preview and the
server's authoritative commit never disagree.
"""

import inspect
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch


def _ensure_frappe_stub():
	try:
		import frappe
	except ModuleNotFoundError:
		frappe = types.ModuleType("frappe")
		frappe.__path__ = []
		frappe._ = lambda value, *args: value
		frappe.PermissionError = PermissionError
		frappe.ValidationError = ValueError
		frappe.DoesNotExistError = LookupError

		def _throw(msg, exc=ValueError):
			if isinstance(exc, type) and issubclass(exc, BaseException):
				raise exc(msg)
			raise ValueError(msg)

		def _whitelist(*args, **kwargs):
			def _mark(fn):
				fn._whitelisted = True
				return fn

			return _mark

		frappe.throw = _throw
		frappe.whitelist = _whitelist
		frappe.get_all = lambda *args, **kwargs: []
		frappe.get_list = lambda *args, **kwargs: frappe.get_all(*args, **kwargs)
		frappe.get_doc = lambda *args, **kwargs: None
		frappe.has_permission = lambda *args, **kwargs: True
		frappe.set_user = lambda *args, **kwargs: None
		frappe.generate_hash = lambda length=6: "0" * length
		frappe.flags = types.SimpleNamespace()
		frappe.db = types.SimpleNamespace(
			exists=lambda *args, **kwargs: False,
			get_value=lambda *args, **kwargs: None,
			set_value=lambda *args, **kwargs: None,
			rollback=lambda *args, **kwargs: None,
		)
		frappe.tests = types.ModuleType("frappe.tests")
		frappe.tests.UnitTestCase = unittest.TestCase
		frappe_utils = types.ModuleType("frappe.utils")
		frappe_utils.flt = lambda value: float(value or 0)
		frappe_utils.cint = lambda value, default=0: int(float(value)) if value is not None else default
		frappe_utils.today = lambda: "2026-01-01"
		frappe_utils.getdate = lambda value=None: (
			value
			if isinstance(value, date)
			else date.fromisoformat(str(value)[:10])
			if value
			else date(2026, 1, 1)
		)
		sys.modules["frappe"] = frappe
		sys.modules["frappe.tests"] = frappe.tests
		sys.modules["frappe.utils"] = frappe_utils
	else:
		frappe_utils = sys.modules.get("frappe.utils")
		if frappe_utils is not None and not hasattr(frappe_utils, "getdate"):
			frappe_utils.getdate = lambda value=None: (
				value
				if isinstance(value, date)
				else date.fromisoformat(str(value)[:10])
				if value
				else date(2026, 1, 1)
			)
		for attr, default in (
			("get_all", lambda *args, **kwargs: []),
			("get_list", lambda *args, **kwargs: frappe.get_all(*args, **kwargs)),
			("get_doc", lambda *args, **kwargs: None),
			("has_permission", lambda *args, **kwargs: True),
			("flags", types.SimpleNamespace()),
			(
				"db",
				types.SimpleNamespace(
					exists=lambda *args, **kwargs: False,
					get_value=lambda *args, **kwargs: None,
					set_value=lambda *args, **kwargs: None,
					rollback=lambda *args, **kwargs: None,
				),
			),
		):
			if not hasattr(frappe, attr):
				setattr(frappe, attr, default)
	return frappe


_ensure_frappe_stub()

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


def _cpm_t(
	name,
	start,
	end,
	status="Open",
	progress=0,
	type="Activity",
	act_start_date=None,
	act_end_date=None,
):
	return {
		"name": name,
		"type": type,
		"start": start,
		"end": end,
		"status": status,
		"progress": progress,
		"act_start_date": act_start_date,
		"act_end_date": act_end_date,
	}


class TestCriticalPath(unittest.TestCase):
	def test_parallel_paths_report_total_and_free_float(self):
		tasks = {
			"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 11)),
			"B": _cpm_t("B", date(2026, 1, 1), date(2026, 1, 5)),
			"D": _cpm_t("D", date(2026, 1, 1), date(2026, 1, 3)),
			"C": _cpm_t("C", date(2026, 1, 1), date(2026, 1, 6)),
		}
		result = eng.compute_cpm(
			tasks,
			[_e("A", "C"), _e("B", "D"), _e("D", "C")],
			date(2026, 1, 1),
		)

		by_task = {row["task"]: row for row in result}
		self.assertEqual(by_task["A"]["total_float"], 0)
		self.assertEqual(by_task["A"]["free_float"], 0)
		self.assertEqual(by_task["B"]["total_float"], 4)
		self.assertEqual(by_task["B"]["free_float"], 0)
		self.assertEqual(by_task["D"]["total_float"], 4)
		self.assertEqual(by_task["D"]["free_float"], 4)
		self.assertEqual(by_task["C"]["total_float"], 0)
		self.assertEqual(by_task["C"]["free_float"], 0)
		self.assertTrue(by_task["A"]["critical"])
		self.assertTrue(by_task["C"]["critical"])
		self.assertFalse(by_task["B"]["critical"])
		self.assertFalse(by_task["D"]["critical"])

	def test_ss_and_ff_lags_drive_early_dates(self):
		tasks = {
			"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 11)),
			"B": _cpm_t("B", date(2026, 1, 1), date(2026, 1, 5)),
			"C": _cpm_t("C", date(2026, 1, 1), date(2026, 1, 5)),
		}
		result = eng.compute_cpm(
			tasks,
			[_e("A", "B", type="SS", lag=3), _e("A", "C", type="FF", lag=2)],
			date(2026, 1, 1),
		)

		by_task = {row["task"]: row for row in result}
		self.assertEqual(by_task["B"]["early_start"], "2026-01-04")
		self.assertEqual(by_task["B"]["early_finish"], "2026-01-08")
		self.assertEqual(by_task["C"]["early_start"], "2026-01-09")
		self.assertEqual(by_task["C"]["early_finish"], "2026-01-13")
		self.assertEqual(by_task["A"]["late_finish"], "2026-01-11")
		self.assertEqual(by_task["A"]["total_float"], 0)
		self.assertEqual(by_task["B"]["total_float"], 5)
		self.assertEqual(by_task["C"]["total_float"], 0)

	def test_milestone_has_zero_duration_and_single_date(self):
		tasks = {
			"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 5)),
			"M": _cpm_t("M", None, date(2026, 1, 5), type="Milestone"),
		}
		result = eng.compute_cpm(tasks, [_e("A", "M")], date(2026, 1, 1))
		milestone = next(row for row in result if row["task"] == "M")

		self.assertEqual(milestone["early_start"], "2026-01-05")
		self.assertEqual(milestone["early_finish"], "2026-01-05")
		self.assertEqual(milestone["late_start"], "2026-01-05")
		self.assertEqual(milestone["late_finish"], "2026-01-05")
		self.assertTrue(milestone["critical"])

	def test_finish_constraint_can_produce_negative_float(self):
		tasks = {"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 11))}
		row = eng.compute_cpm(tasks, [], date(2026, 1, 1), finish_constraint=date(2026, 1, 6))[0]

		self.assertEqual(row["total_float"], -5)
		self.assertEqual(row["free_float"], -5)
		self.assertTrue(row["critical"])

	def test_cycle_is_refused_with_stable_schedule_code(self):
		tasks = {
			"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 3)),
			"B": _cpm_t("B", date(2026, 1, 3), date(2026, 1, 5)),
		}
		with self.assertRaisesRegex(ValueError, "SCHEDULE_CYCLE"):
			eng.compute_cpm(tasks, [_e("A", "B"), _e("B", "A")], date(2026, 1, 1))

	def test_completed_task_is_fixed_to_actuals_before_data_date(self):
		tasks = {
			"A": _cpm_t(
				"A",
				date(2026, 1, 1),
				date(2026, 1, 11),
				status="Completed",
				progress=100,
				act_start_date=date(2026, 1, 2),
				act_end_date=date(2026, 1, 8),
			),
			"B": _cpm_t(
				"B",
				date(2026, 1, 5),
				date(2026, 1, 15),
				status="Working",
				progress=60,
				act_start_date=date(2026, 1, 6),
			),
			"C": _cpm_t("C", date(2026, 1, 15), date(2026, 1, 18)),
		}
		result = eng.compute_cpm(tasks, [_e("A", "B"), _e("B", "C")], date(2026, 1, 10))
		by_task = {row["task"]: row for row in result}

		self.assertEqual(by_task["A"]["early_start"], "2026-01-02")
		self.assertEqual(by_task["A"]["early_finish"], "2026-01-08")
		self.assertFalse(by_task["A"]["critical"])
		self.assertEqual(by_task["B"]["early_start"], "2026-01-10")
		self.assertEqual(by_task["B"]["early_finish"], "2026-01-14")
		self.assertTrue(by_task["B"]["critical"])
		self.assertEqual(by_task["C"]["early_start"], "2026-01-14")
		self.assertEqual(by_task["C"]["early_finish"], "2026-01-17")
		self.assertTrue(by_task["C"]["critical"])

	def test_not_started_task_keeps_a_planned_start_after_data_date(self):
		tasks = {
			"A": _cpm_t("A", date(2026, 1, 20), date(2026, 1, 25)),
		}
		row = eng.compute_cpm(tasks, [], date(2026, 1, 10))[0]

		self.assertEqual(row["early_start"], "2026-01-20")
		self.assertEqual(row["early_finish"], "2026-01-25")

	def test_get_critical_path_uses_the_schedule_loader_without_writing(self):
		tasks = {"A": _cpm_t("A", date(2026, 1, 1), date(2026, 1, 3))}
		with patch.object(eng, "_load_graph", return_value=(tasks, [])) as load_graph:
			result = eng.get_critical_path("PROJ-1", data_date="2026-01-01")

		load_graph.assert_called_once_with("PROJ-1")
		self.assertEqual(result[0]["task"], "A")
		self.assertEqual(
			set(result[0]),
			{
				"task",
				"early_start",
				"early_finish",
				"late_start",
				"late_finish",
				"total_float",
				"free_float",
				"critical",
			},
		)

	def test_schedule_loader_uses_permission_respecting_lists(self):
		class Row:
			def __init__(self, **values):
				self.__dict__.update(values)

		task_rows = [
			Row(
				name="A",
				type="Activity",
				exp_start_date="2026-01-01",
				exp_end_date="2026-01-03",
				act_start_date=None,
				act_end_date=None,
				progress=0,
				status="Open",
				parent_task=None,
			)
		]
		edge_rows = []

		def get_list(doctype, **kwargs):
			return task_rows if doctype == "Task" else edge_rows

		with (
			patch.object(eng.frappe, "get_list", side_effect=get_list) as get_list_mock,
			patch.object(eng.frappe, "get_all", side_effect=AssertionError("bypass")),
		):
			eng._load_graph("PROJ-1")

		self.assertEqual(get_list_mock.call_count, 2)
		self.assertEqual(
			[get_list_mock.call_args_list[0].args[0], get_list_mock.call_args_list[1].args[0]],
			["Task", "Task Depends On"],
		)


class TestScheduleEngine(UnitTestCase):
	def test_reschedule_asap_uses_native_truthiness_and_preserves_default(self):
		parameter = inspect.signature(eng.reschedule_downstream).parameters["asap"]
		self.assertEqual(parameter.annotation, bool | int)
		self.assertIs(parameter.default, False)

		root = type("Root", (), {"project": "PROJ-1"})()
		tasks = {"root": _t("root")}
		seen = []

		def compute_cascade(*args, **kwargs):
			seen.append(kwargs["asap"])
			return []

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(eng, "_load_graph", return_value=(tasks, [])),
			patch.object(eng, "compute_cascade", side_effect=compute_cascade),
			patch.object(eng, "recompute_schedule_conflicts"),
		):
			for value in (True, 1, "1", "yes"):
				eng.reschedule_downstream("root", asap=value)
			eng.reschedule_downstream("root")

		self.assertEqual(seen, [True, True, True, True, False])

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
			"A",
			tasks,
			[_e("A", "B", type="SS", lag=5)],
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
			eng.compute_cascade("A", tasks, edges, {"start": date(2026, 1, 1), "end": date(2026, 1, 20)}),
			[],
		)

	def test_ff_tracks_predecessor_end_and_preserves_duration(self):
		tasks = {
			"A": _t("A", start=date(2026, 1, 1), end=date(2026, 1, 20)),
			"B": _t("B", start=date(2026, 1, 3), end=date(2026, 1, 8)),
		}
		moves = eng.compute_cascade(
			"A",
			tasks,
			[_e("A", "B", type="FF", lag=2)],
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

		def get_list(doctype, **kwargs):
			if doctype == "Task":
				return [
					Row(
						name="1.0",
						type="Activity",
						exp_start_date="2026-01-01",
						exp_end_date="2026-01-10",
						status="Open",
						parent_task=None,
					),
					Row(
						name="1.3",
						type="Activity",
						exp_start_date="2026-01-02",
						exp_end_date="2026-01-09",
						status="Open",
						parent_task="1.0",
					),
					Row(
						name="2.1",
						type="Activity",
						exp_start_date="2026-01-01",
						exp_end_date="2026-01-10",
						status="Open",
						parent_task=None,
					),
					Row(
						name="2.2",
						type="Activity",
						exp_start_date="2026-01-10",
						exp_end_date="2026-01-12",
						status="Open",
						parent_task=None,
					),
				]
			return [
				Row(parent="1.0", task="1.3", dependency_type="FS", lag_days=0),
				Row(parent="2.2", task="2.1", dependency_type="FS", lag_days=0),
			]

		with patch.object(eng.frappe, "get_list", side_effect=get_list):
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
			{"root": 5, "2.2": 5, "3.1": 5, "4.1": 5, "float-3": 2},
		)

	def test_root_only_slip_returns_root_move_snapshots_and_saves_root_once(self):
		class Root:
			name = "root"
			project = "PROJ-1"
			exp_start_date = date(2026, 3, 1)
			exp_end_date = date(2026, 3, 10)

			def __init__(self):
				self.flags = type("Flags", (), {"ignore_permissions": False})()
				self.saves = 0

			def save(self, **kwargs):
				self.saves += 1

		root = Root()
		tasks = {"root": _t("root", start=root.exp_start_date, end=root.exp_end_date)}
		eng.frappe.flags = type("Flags", (), {})()

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(eng, "_load_graph", return_value=(tasks, [])),
			patch.object(eng, "recompute_schedule_conflicts"),
			patch("buildsuite_core.api.schedule_snapshot.capture_snapshot") as capture_snapshot,
		):
			preview = eng.reschedule_downstream("root", dry_run=1, slip_days=5)
			commit = eng.reschedule_downstream("root", dry_run=0, slip_days=5)

		self.assertEqual(preview, commit)
		self.assertEqual(
			preview["moves"],
			[
				{
					"task": "root",
					"old_start": "2026-03-01",
					"old_end": "2026-03-10",
					"new_start": "2026-03-06",
					"new_end": "2026-03-15",
					"reason": "slip 5 days",
					"is_milestone": False,
				}
			],
		)
		capture_snapshot.assert_called_once_with(
			"PROJ-1", "Undo", trigger="reschedule_downstream", root_task="root"
		)
		self.assertEqual(root.saves, 1)

	def test_apply_moves_writes_milestone_start_before_normalization_hook(self):
		class StubDoc:
			def __init__(self, name, type, start, end):
				self.name = name
				self.type = type
				self.exp_start_date = start
				self.exp_end_date = end
				self.saves = []

			def save(self, **kwargs):
				# Mimic normalize_milestone_task: a milestone with a start collapses
				# its end to that start during save.
				if self.type == "Milestone" and self.exp_start_date:
					self.exp_end_date = self.exp_start_date
				self.saves.append((self.exp_start_date, self.exp_end_date))

		root = StubDoc("root", "Activity", date(2026, 3, 1), date(2026, 3, 10))
		milestone = StubDoc("milestone", "Milestone", date(2026, 3, 12), date(2026, 3, 12))
		eng.frappe.flags = type("Flags", (), {})()

		with patch.object(eng.frappe, "get_doc", return_value=milestone):
			eng._apply_moves(
				root,
				[
					{
						"task": "milestone",
						"new_start": None,
						"new_end": "2026-03-20",
						"is_milestone": True,
					}
				],
			)

		self.assertEqual(milestone.saves, [("2026-03-20", "2026-03-20")])

	def test_slip_days_rejects_root_with_both_expected_dates_missing(self):
		root = type(
			"Root",
			(),
			{
				"project": "PROJ-1",
				"exp_start_date": None,
				"exp_end_date": None,
			},
		)()

		def throw(message, exc=None):
			raise ValueError(message)

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(eng, "_load_graph", return_value=({"root": _t("root")}, [])),
			patch.object(eng.frappe, "throw", side_effect=throw),
			patch.object(eng, "_apply_moves") as apply_moves,
			patch.object(eng, "recompute_schedule_conflicts"),
			self.assertRaisesRegex(
				ValueError, "slip_days requires the root task to have at least one expected date"
			),
		):
			eng.reschedule_downstream("root", dry_run=0, slip_days=5)

		apply_moves.assert_not_called()

	def test_zero_day_slip_is_a_no_op(self):
		root = type(
			"Root",
			(),
			{
				"name": "root",
				"project": "PROJ-1",
				"exp_start_date": date(2026, 3, 1),
				"exp_end_date": date(2026, 3, 10),
			},
		)()
		eng.frappe.flags = type("Flags", (), {})()

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(
				eng,
				"_load_graph",
				return_value=(
					{"root": _t("root", start=root.exp_start_date, end=root.exp_end_date)},
					[],
				),
			),
			patch.object(eng, "recompute_schedule_conflicts"),
			patch("buildsuite_core.api.schedule_snapshot.capture_snapshot") as capture_snapshot,
		):
			result = eng.reschedule_downstream("root", dry_run=0, slip_days=0)

		self.assertEqual(result, {"moves": []})
		capture_snapshot.assert_not_called()

	def test_explicit_single_task_move_has_no_root_move_or_snapshot(self):
		root = type(
			"Root",
			(),
			{
				"name": "root",
				"project": "PROJ-1",
				"exp_start_date": date(2026, 1, 1),
				"exp_end_date": date(2026, 1, 10),
			},
		)()
		eng.frappe.flags = type("Flags", (), {})()

		with (
			patch.object(eng.frappe, "get_doc", return_value=root),
			patch.object(
				eng,
				"_load_graph",
				return_value=(
					{"root": _t("root", start=root.exp_start_date, end=root.exp_end_date)},
					[],
				),
			),
			patch.object(eng, "_apply_moves"),
			patch.object(eng, "recompute_schedule_conflicts"),
			patch("buildsuite_core.api.schedule_snapshot.capture_snapshot") as capture_snapshot,
		):
			result = eng.reschedule_downstream(
				"root",
				new_start="2026-02-01",
				new_end="2026-02-10",
				dry_run=0,
				asap=0,
			)

		self.assertEqual(result, {"moves": []})
		capture_snapshot.assert_not_called()

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

		with patch.object(eng.frappe, "get_list", side_effect=get_all):
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
			eng.reschedule_downstream("root", new_start="2026-03-02", dry_run=1, slip_days=5)

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
