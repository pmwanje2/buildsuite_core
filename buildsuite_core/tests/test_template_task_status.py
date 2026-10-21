"""Template Tasks keep native status Template through BuildSuite status hooks."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[2]


def _load_task_module():
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda: (lambda function: function)
	frappe.__path__ = []
	utils = types.ModuleType("frappe.utils")
	utils.flt = lambda value: float(value or 0)
	utils.getdate = lambda value: value
	utils.today = lambda: "2026-10-20"
	frappe._ = lambda value: value

	spec = importlib.util.spec_from_file_location(
		"buildsuite_template_task_under_test", ROOT / "buildsuite_core" / "utils" / "task.py"
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
		spec.loader.exec_module(module)
	return module


def _load_restore_patch(frappe):
	spec = importlib.util.spec_from_file_location(
		"buildsuite_restore_template_task_status_under_test",
		ROOT / "buildsuite_core" / "patches" / "restore_template_task_status.py",
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe}):
		spec.loader.exec_module(module)
	return module


class _TaskDoc:
	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def get_doc_before_save(self):
		return None


def _template_task(*, exp_end_date):
	return _TaskDoc(
		is_template=1,
		project=None,
		status="Template",
		task_status="Yet To Start",
		progress=0,
		exp_end_date=exp_end_date,
		exp_start_date=None,
	)


class TestTemplateTaskStatus(unittest.TestCase):
	def test_template_task_keeps_template_status_through_status_hooks(self):
		task = _load_task_module()
		cases = (
			("dated", "2020-01-01"),
			("undated", None),
		)
		for function in (task.update_task_status, task.update_task_status_insert):
			for label, exp_end_date in cases:
				with self.subTest(function=function.__name__, shape=label):
					doc = _template_task(exp_end_date=exp_end_date)
					function(doc)
					self.assertEqual(doc.status, "Template")
					self.assertEqual(doc.task_status, "Yet To Start")

	def test_update_delayed_tasks_excludes_templates(self):
		task = _load_task_module()
		captured = []

		def get_all(*args, **kwargs):
			self.assertEqual(args, ("Task",))
			captured.append(kwargs.get("filters"))
			return []

		task.frappe.get_all = get_all
		task.frappe.get_doc = Mock(side_effect=AssertionError("delayed sweep loaded a task"))
		task.frappe.db = types.SimpleNamespace(commit=lambda: None)

		task.update_delayed_tasks()

		self.assertEqual(len(captured), 2)
		for filters in captured:
			self.assertIn(["is_template", "=", 0], filters)


class TestTemplatePredecessorGate(unittest.TestCase):
	def test_predecessor_gate_never_rewrites_a_template(self):
		# Review: a template left at task_status In Delay with an open predecessor
		# must keep native status Template through the last Task validate hook.
		task = _load_task_module()
		schedule = types.ModuleType("buildsuite_core.api.schedule")
		schedule.incomplete_fs_predecessor = lambda *args, **kwargs: "TASK-PRED"
		doc = _template_task(exp_end_date=None)
		doc.task_status = "In Delay"
		doc.name = "TASK-TPL"
		doc.depends_on = []
		with patch.dict(sys.modules, {
			"buildsuite_core": types.ModuleType("buildsuite_core"),
			"buildsuite_core.api": types.ModuleType("buildsuite_core.api"),
			"buildsuite_core.api.schedule": schedule,
		}):
			task.enforce_predecessor_gate(doc)
		self.assertEqual(doc.status, "Template")
		self.assertEqual(doc.task_status, "In Delay")


class TestRestoreTemplateTaskStatusPatch(unittest.TestCase):
	def test_patch_is_registered(self):
		text = (ROOT / "buildsuite_core" / "patches.txt").read_text(encoding="utf-8")
		self.assertIn("buildsuite_core.patches.restore_template_task_status", text)

	def test_db_sets_template_status_without_updating_modified(self):
		frappe = types.ModuleType("frappe")
		frappe.get_all = Mock(return_value=["TASK-TPL-A", "TASK-TPL-B"])
		frappe.db = Mock()
		module = _load_restore_patch(frappe)

		module.execute()

		self.assertEqual(frappe.get_all.call_args.kwargs["filters"], {"is_template": 1})
		self.assertEqual(frappe.get_all.call_args.kwargs["pluck"], "name")
		self.assertEqual(
			frappe.db.set_value.call_args_list,
			[
				call("Task", "TASK-TPL-A", {"status": "Template", "task_status": "Yet To Start"}, update_modified=False),
				call("Task", "TASK-TPL-B", {"status": "Template", "task_status": "Yet To Start"}, update_modified=False),
			],
		)

	def test_patch_repairs_once_and_second_run_is_a_no_op(self):
		rows = {
			"TASK-TPL-A": {"is_template": 1, "status": "Overdue", "task_status": "In Delay"},
			"TASK-TPL-B": {"is_template": 1, "status": "Template", "task_status": "Yet To Start"},
			"TASK-REAL": {"is_template": 0, "status": "Overdue", "task_status": "In Delay"},
		}

		def get_all(doctype, filters=None, or_filters=None, pluck=None):
			assert doctype == "Task" and pluck == "name"
			out = []
			for name, row in rows.items():
				if any(row[key] != value for key, value in (filters or {}).items()):
					continue
				if or_filters and not any(row[key] != cond[1] for key, cond in or_filters.items()):
					continue
				out.append(name)
			return out

		def set_value(doctype, name, values, update_modified=True):
			assert update_modified is False
			rows[name].update(values)

		frappe = types.ModuleType("frappe")
		frappe.get_all = get_all
		frappe.db = types.SimpleNamespace(set_value=Mock(side_effect=set_value))
		module = _load_restore_patch(frappe)
		module.execute()
		self.assertEqual(frappe.db.set_value.call_count, 1)
		self.assertEqual(rows["TASK-TPL-A"], {"is_template": 1, "status": "Template", "task_status": "Yet To Start"})
		self.assertEqual(rows["TASK-REAL"]["status"], "Overdue")
		module.execute()
		self.assertEqual(frappe.db.set_value.call_count, 1)

	def test_patch_selects_templates_with_stale_status_or_task_status(self):
		frappe = types.ModuleType("frappe")
		frappe.get_all = Mock(return_value=[])
		frappe.db = Mock()
		module = _load_restore_patch(frappe)
		module.execute()
		module.execute()
		self.assertEqual(frappe.get_all.call_args.kwargs["or_filters"],
			{"status": ["!=", "Template"], "task_status": ["!=", "Yet To Start"]})
		self.assertEqual(frappe.get_all.call_args.kwargs["filters"], {"is_template": 1})
		frappe.db.set_value.assert_not_called()


if __name__ == "__main__":
	unittest.main()
