"""Focused tests for the NULL-safe Task status and overdue repair patch."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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
		"buildsuite_task_under_test", ROOT / "buildsuite_core" / "utils" / "task.py"
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
		spec.loader.exec_module(module)
	return module


def _load_patch_module(frappe):
	spec = importlib.util.spec_from_file_location(
		"buildsuite_overdue_patch_under_test",
		ROOT / "buildsuite_core" / "patches" / "fix_undated_overdue_tasks.py",
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe}):
		spec.loader.exec_module(module)
	return module


class TestTaskStatusGuard(unittest.TestCase):
	def test_null_progress_is_zero_for_edit_and_insert_status_mapping(self):
		task = _load_task_module()
		for function in (task.update_task_status, task.update_task_status_insert):
			with self.subTest(function=function.__name__):
				doc = types.SimpleNamespace(
					progress=None,
					task_status="In Delay",
					exp_end_date=None,
					exp_start_date=None,
					status=None,
					get_doc_before_save=lambda: None,
				)
				function(doc)
				self.assertEqual(doc.status, "Open")


class TestUndatedOverduePatch(unittest.TestCase):
	def test_one_failed_save_is_logged_and_does_not_abort_other_rows(self):
		frappe = types.ModuleType("frappe")
		frappe.get_all = Mock(return_value=["TASK-FAIL", "TASK-OK"])
		frappe.log_error = Mock()
		saved = []

		class Task:
			def __init__(self, name):
				self.name = name

			def save(self, *, ignore_permissions):
				self.ignore_permissions = ignore_permissions
				if self.name == "TASK-FAIL":
					raise RuntimeError("simulated validation failure")
				saved.append(self.name)

		frappe.get_doc = lambda doctype, name: Task(name)
		patch_module = _load_patch_module(frappe)

		patch_module.execute()

		self.assertEqual(frappe.get_all.call_args.args, ("Task",))
		self.assertEqual(saved, ["TASK-OK"])
		self.assertEqual(
			frappe.get_all.call_args.kwargs["filters"],
			{"status": "Overdue", "exp_end_date": ["is", "not set"]},
		)
		self.assertLessEqual(frappe.get_all.call_args.kwargs["limit_page_length"], 500)
		self.assertEqual(frappe.log_error.call_count, 1)
		self.assertIn("TASK-FAIL", frappe.log_error.call_args.args[0])
		self.assertNotIn("simulated validation failure", frappe.log_error.call_args.args[0])


if __name__ == "__main__":
	unittest.main()
