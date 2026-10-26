# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""Unit contract for the native Project Template -> operative WBS import."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PROJECT_TEMPLATE_TASK_FIELDS = frozenset({"task", "subject", "custom_work_package_code", "custom_stage"})


class FakeImportError(Exception):
	pass


class FakeValidationError(FakeImportError):
	pass


class Flags(dict):
	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


class Row:
	def __init__(self, **values):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class ProjectTemplateTaskRow(Row):
	"""Enforce ERPNext Project Template Task + BuildSuite custom field names."""

	def __init__(self, **values):
		unexpected = set(values) - PROJECT_TEMPLATE_TASK_FIELDS
		if unexpected:
			raise AssertionError(f"Unexpected Project Template Task fields: {sorted(unexpected)}")
		super().__init__(**values)

	def __getattr__(self, key):
		if key not in PROJECT_TEMPLATE_TASK_FIELDS:
			raise AttributeError(f"Project Template Task has no field {key!r}")
		return self.__dict__.get(key)

	def get(self, key, default=None):
		if key not in PROJECT_TEMPLATE_TASK_FIELDS:
			raise KeyError(f"Project Template Task has no field {key!r}")
		return self.__dict__.get(key, default)


class FakeDoc:
	def __init__(self, state, **values):
		self._state = state
		self.__dict__.update(values)
		self.flags = Flags()
		self.name = getattr(self, "name", None)
		self.depends_on = getattr(self, "depends_on", []) or []

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def check_permission(self, _ptype):
		return None

	def append(self, field, values):
		getattr(self, field).append(Row(**values))

	def insert(self, **_kwargs):
		if self.doctype == "Task" and self.subject == self._state.fail_subject:
			self._state.frappe_ref.local.message_log.append("leaked validation log")
			raise self._state.fail_error
		self._state.cascade_flags_seen.append(bool(self._state.frappe_ref.flags.get("in_schedule_cascade")))
		if not self.name:
			self._state.sequence += 1
			self.name = f"{self.doctype.replace(' ', '-')}-{self._state.sequence}"
		self._state.inserted.append(self)
		return self

	def save(self, **_kwargs):
		self._state.cascade_flags_seen.append(bool(self._state.frappe_ref.flags.get("in_schedule_cascade")))
		self._state.saved.append(self)
		return self


class FakeDb:
	def __init__(self, state):
		self._state = state
		self.rollback_calls = []
		self._snapshots = {}

	def get_value(self, doctype, _filters, fieldname=None, **_kwargs):
		if doctype == "Project Template":
			return self._state.template_name
		if doctype == "Project":
			return self._state.project.company
		return None

	def savepoint(self, name):
		self._snapshots[name] = (
			list(self._state.inserted),
			self._state.sequence,
		)

	def rollback(self, *, save_point=None):
		self.rollback_calls.append(save_point)
		if save_point in self._snapshots:
			inserted, sequence = self._snapshots[save_point]
			self._state.inserted = inserted
			self._state.sequence = sequence


class FakeState:
	def __init__(self):
		self.template_name = "TPL-1"
		self.project = None
		self.template = None
		self.template_tasks = {}
		self.existing_wps = []
		self.inserted = []
		self.saved = []
		self.sequence = 0
		self.fail_subject = None
		self.fail_error = RuntimeError("raw database detail must not reach the caller")
		self.error_logs = []
		self.cascade_flags_seen = []
		self.frappe_ref = None

		# The template Task fields below are deliberately native ERPNext fields:
		# Project Template Task has only the linked `task`; `start`, `duration`,
		# and `depends_on` belong to the linked Task master.
		native_predecessor = Row(
			name="TPL-PRED",
			doctype="Task",
			subject="Pred",
			priority="High",
			expected_time=8,
			start=2,
			duration=4,
			depends_on=[],
		)
		native_successor = Row(
			name="TPL-SUCC",
			doctype="Task",
			subject="Succ",
			priority="Medium",
			expected_time=16,
			start=None,
			duration=None,
			depends_on=[Row(task="TPL-PRED", dependency_type="SS", lag_days=2)],
		)
		self.template_tasks = {
			"TPL-PRED": native_predecessor,
			"TPL-SUCC": native_successor,
		}
		self.template = Row(
			doctype="Project Template",
			project_category="Commercial",
			custom_work_packages=[
				Row(code="WP-KEEP", work_package_name="Kept", budget=10, sort_order=1, description=None),
				Row(code="WP-NEW", work_package_name="New", budget=20, sort_order=2, description=None),
			],
			custom_stages=[
				Row(
					stage_name="Stage A",
					offset_start_days=10,
					offset_end_days=20,
					planned_task_count=2,
					planned_completion_pct=100,
					description=None,
				)
			],
			tasks=[
				ProjectTemplateTaskRow(
					task="TPL-PRED",
					subject="Pred",
					custom_work_package_code="WP-NEW",
					custom_stage="Stage A",
				),
				ProjectTemplateTaskRow(
					task="TPL-SUCC",
					subject="Succ",
					custom_work_package_code="WP-NEW",
					custom_stage="Stage A",
				),
			],
		)

	@property
	def frappe(self):
		frappe = types.ModuleType("frappe")
		frappe.__path__ = []
		frappe._ = lambda value: value
		frappe.ValidationError = FakeValidationError
		frappe.PermissionError = PermissionError
		frappe.flags = Flags()
		frappe.local = types.SimpleNamespace(message_log=["prior message"])
		frappe.db = FakeDb(self)
		frappe.whitelist = lambda: lambda function: function
		def throw(message, error_type=None, **_kwargs):
			frappe.local.message_log.append({"message": message})
			raise (error_type or FakeImportError)(message)

		frappe.throw = throw
		frappe.get_traceback = lambda: "traceback"
		frappe.log_error = lambda *args, **kwargs: self.error_logs.append((args, kwargs))
		frappe.get_all = self.get_all
		frappe.get_doc = self.get_doc
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		utils.strip_html_tags = lambda value: str(value).replace("<b>", "").replace("</b>", "")
		utils.today = lambda: "2026-01-10"
		utils.getdate = lambda value: date.fromisoformat(str(value)[:10]) if value else None
		utils.add_days = lambda value, days: (
			date.fromisoformat(str(value)[:10]) + timedelta(days=int(days or 0))
		).isoformat()
		frappe.utils = utils
		self.frappe_ref = frappe
		return frappe

	def get_doc(self, doctype_or_values, name=None):
		if isinstance(doctype_or_values, dict):
			return FakeDoc(self, **deepcopy(doctype_or_values))
		if doctype_or_values == "Project":
			return self.project
		if doctype_or_values == "Project Template":
			return self.template
		if doctype_or_values == "Task":
			return self.template_tasks[name]
		raise AssertionError((doctype_or_values, name))

	def get_all(self, doctype, filters=None, fields=None, **_kwargs):
		if doctype == "Work Package":
			return [Row(name=wp.name, code=wp.code) for wp in self.existing_wps]
		if doctype == "Task":
			return []
		raise AssertionError((doctype, filters, fields))


def _load_project_module(state):
	model = types.ModuleType("frappe.model")
	model.__path__ = []
	document = types.ModuleType("frappe.model.document")
	document.Document = object
	frappe = state.frappe
	spec = importlib.util.spec_from_file_location(
		"buildsuite_project_import_under_test",
		ROOT / "buildsuite_core" / "utils" / "project.py",
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		sys.modules,
		{
			"frappe": frappe,
			"frappe.utils": frappe.utils,
			"frappe.model": model,
			"frappe.model.document": document,
		},
	):
		spec.loader.exec_module(module)
	return module


def _load_task_override_module():
	frappe = types.ModuleType("frappe")
	frappe.flags = Flags()
	frappe.__path__ = []
	erpnext = types.ModuleType("erpnext")
	erpnext.__path__ = []
	projects = types.ModuleType("erpnext.projects")
	projects.__path__ = []
	doctype = types.ModuleType("erpnext.projects.doctype")
	doctype.__path__ = []
	task_package = types.ModuleType("erpnext.projects.doctype.task")
	task_package.__path__ = []
	task_module = types.ModuleType("erpnext.projects.doctype.task.task")
	task_module.Task = type("NativeTask", (), {})
	spec = importlib.util.spec_from_file_location(
		"buildsuite_task_override_under_test",
		ROOT / "buildsuite_core" / "overrides" / "task.py",
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		sys.modules,
		{
			"frappe": frappe,
			"erpnext": erpnext,
			"erpnext.projects": projects,
			"erpnext.projects.doctype": doctype,
			"erpnext.projects.doctype.task": task_package,
			"erpnext.projects.doctype.task.task": task_module,
		},
	):
		spec.loader.exec_module(module)
	return module, frappe


class TestProjectTemplateImport(unittest.TestCase):
	def setUp(self):
		self.state = FakeState()
		self.state.project = FakeDoc(
			self.state,
			doctype="Project",
			name="PROJECT-1",
			project_category="Commercial",
			expected_start_date="2026-01-10",
			expected_end_date="2027-01-10",
			company="Company A",
		)
		self.state.existing_wps = [Row(name="WP-EXISTING", code="WP-KEEP")]
		self.module = _load_project_module(self.state)

	def _import(self, **kwargs):
		return self.module.import_project_template("PROJECT-1", work_packages=1, stages=0, tasks=1, **kwargs)

	def test_project_creation_remains_lenient_and_tasks_remain_undated(self):
		self.state.project.custom_seed_default_work_packages = 1
		self.state.project.custom_seed_default_tasks = 1
		self.state.fail_subject = "Succ"

		self.module.seed_from_template_on_insert(self.state.project)

		tasks = [doc for doc in self.state.inserted if doc.doctype == "Task"]
		self.assertEqual([task.subject for task in tasks], ["Pred"])
		self.assertIsNone(tasks[0].get("exp_start_date"))
		self.assertIsNone(tasks[0].get("exp_end_date"))
		self.assertEqual(len(self.state.error_logs), 1)
		self.assertEqual(self.module.frappe.db.rollback_calls, [])

	def test_preview_writes_nothing_and_reports_work_packages_by_code(self):
		result = self._import(test_run=1)

		self.assertEqual(result["work_packages_created"], ["WP-NEW"])
		self.assertEqual(result["work_packages_reused"], ["WP-KEEP"])
		self.assertEqual(result["tasks"], 2)
		self.assertEqual(result["computed_finish"], "2026-01-30")
		self.assertEqual(
			[
				(row["template_task"], row["exp_start_date"], row["exp_end_date"])
				for row in result["task_rows"]
			],
			[
				("TPL-PRED", "2026-01-12", "2026-01-16"),
				("TPL-SUCC", "2026-01-20", "2026-01-30"),
			],
		)
		self.assertEqual(
			result["dependencies"],
			[
				{
					"task": "TPL-SUCC",
					"depends_on": "TPL-PRED",
					"dependency_type": "SS",
					"lag_days": 2,
				}
			],
		)
		self.assertEqual(result["skipped"][0]["reason"], "TEMPLATE_WP_REUSED")
		self.assertIn("WP-KEEP", result["skipped"][0]["message"])
		self.assertEqual(self.state.inserted, [])
		self.assertEqual(self.state.saved, [])

	def test_existing_caller_without_test_run_still_imports(self):
		result = self._import()

		self.assertEqual(result["tasks"], 2)
		self.assertEqual(len(self.state.inserted), 3)
		self.assertEqual(len(self.state.saved), 1)

	def test_tasks_use_native_offsets_or_stage_offsets_and_copy_dependencies(self):
		result = self._import(test_run=0)

		self.assertEqual(result["tasks"], 2)
		self.assertEqual(result["computed_finish"], "2026-01-30")
		created = {task.subject: task for task in self.state.inserted if task.doctype == "Task"}
		self.assertEqual(created["Pred"].exp_start_date, "2026-01-12")
		self.assertEqual(created["Pred"].exp_end_date, "2026-01-16")
		self.assertEqual(created["Succ"].exp_start_date, "2026-01-20")
		self.assertEqual(created["Succ"].exp_end_date, "2026-01-30")
		self.assertEqual(created["Succ"].depends_on[0].task, created["Pred"].name)
		self.assertEqual(created["Succ"].depends_on[0].dependency_type, "SS")
		self.assertEqual(created["Succ"].depends_on[0].lag_days, 2)
		self.assertTrue(self.state.cascade_flags_seen)
		self.assertTrue(all(self.state.cascade_flags_seen))
		self.assertFalse(bool(self.module.frappe.flags.get("in_schedule_cascade")))

	def test_productive_failure_refuses_with_reason_and_rolls_back_everything(self):
		self.state.fail_subject = "Succ"

		with self.assertRaisesRegex(FakeValidationError, "TEMPLATE_TASK_CREATE_FAILED") as raised:
			self._import(test_run=0)

		self.assertIn("Could not import task 'Succ'", str(raised.exception))
		self.assertNotIn("raw database detail", str(raised.exception))
		self.assertEqual(self.state.inserted, [])
		self.assertEqual(self.state.saved, [])
		self.assertEqual(self.module.frappe.db.rollback_calls, ["template_import"])
		self.assertEqual(
			self.module.frappe.local.message_log,
			["prior message", {"message": str(raised.exception)}],
		)
		self.assertEqual(len(self.state.error_logs), 1)

		# Single-string ValidationError strips HTML and skips error logging, while
		# multi-arg ValidationError is sanitized and logged.
		self.state.error_logs.clear()
		self.state.fail_error = FakeValidationError("<b> Start date is in a closed period </b>")
		with self.assertRaisesRegex(FakeValidationError, "TEMPLATE_TASK_CREATE_FAILED") as single_val:
			self._import(test_run=0)
		self.assertIn("Start date is in a closed period", str(single_val.exception))
		self.assertNotIn("<b>", str(single_val.exception))
		self.assertEqual(
			self.module.frappe.local.message_log[-1], {"message": str(single_val.exception)}
		)
		self.assertEqual(self.state.error_logs, [])

		self.state.fail_error = FakeValidationError("Duplicate entry", 1062)
		with self.assertRaisesRegex(FakeValidationError, "TEMPLATE_TASK_CREATE_FAILED") as multi_val:
			self._import(test_run=0)
		self.assertNotIn("1062", str(multi_val.exception))
		self.assertEqual(len(self.state.error_logs), 1)

	def test_task_override_skips_cascade_when_instance_flag_is_set(self):
		module, frappe = _load_task_override_module()
		task = object.__new__(module.BuildSuiteTask)
		task.flags = Flags(in_schedule_cascade=True)

		module.BuildSuiteTask.reschedule_dependent_tasks(task)

		self.assertTrue(task.flags.in_schedule_cascade)
		self.assertIsNone(frappe.flags.get("in_schedule_cascade"))


if __name__ == "__main__":
	unittest.main()
