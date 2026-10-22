"""Company-wide project roles may bypass BuildSuite team scoping."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


def _load_permission_modules():
	frappe = types.ModuleType("frappe")
	frappe.__path__ = []
	frappe.conf = {}
	frappe.get_roles = lambda user: []
	frappe.session = types.SimpleNamespace(user="test@example.com")
	frappe.db = types.SimpleNamespace(
		exists=Mock(return_value=False),
		escape=lambda value: f"'{value}'",
	)
	frappe_utils = types.ModuleType("frappe.utils")
	frappe_utils.get_datetime = lambda value: value
	frappe_utils.now_datetime = lambda: None

	buildsuite = types.ModuleType("buildsuite_core")
	buildsuite.__path__ = []
	permissions = types.ModuleType("buildsuite_core.permissions")
	permissions.__path__ = []
	modules = {
		"frappe": frappe,
		"frappe.utils": frappe_utils,
		"buildsuite_core": buildsuite,
		"buildsuite_core.permissions": permissions,
	}
	loaded = {}
	paths = {
		"project": ROOT / "buildsuite_core" / "permissions" / "project.py",
		"task": ROOT / "buildsuite_core" / "permissions" / "task.py",
		"work_package": ROOT / "buildsuite_core" / "permissions" / "work_package.py",
		"task_progress_entry": ROOT / "buildsuite_core" / "permissions" / "task_progress_entry.py",
		"stage_planning": ROOT / "buildsuite_core" / "permissions" / "stage_planning.py",
	}

	with patch.dict(sys.modules, modules):
		for name, path in paths.items():
			module_name = f"buildsuite_core.permissions.{name}"
			spec = importlib.util.spec_from_file_location(module_name, path)
			module = importlib.util.module_from_spec(spec)
			sys.modules[module_name] = module
			spec.loader.exec_module(module)
			loaded[name] = module

	return frappe, loaded


class TestProjectCompanyRolesScope(unittest.TestCase):
	def test_finance_oversight_role_is_unscoped_and_can_read(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Finance Oversight"]
		frappe.get_roles = lambda user: ["Finance Oversight"]

		self.assertFalse(modules["project"]._is_scoped("finance@example.com"))
		self.assertEqual(modules["project"].get_project_permission_query("finance@example.com"), "")
		self.assertTrue(
			modules["project"].has_project_permission(
				types.SimpleNamespace(name="PROJECT-001"), user="finance@example.com"
			)
		)

	def test_teamed_accountant_listed_in_config_is_unscoped(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["BuildSuite Accountant"]
		frappe.get_roles = lambda user: ["BuildSuite Accountant"]
		frappe.db.exists.return_value = True

		self.assertFalse(modules["project"]._is_scoped("accountant@example.com"))
		self.assertEqual(modules["project"].get_project_permission_query("accountant@example.com"), "")

	def test_project_company_role_exemption_is_inherited_by_project_queries(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Finance Oversight"]
		frappe.get_roles = lambda user: ["Finance Oversight"]

		queries = {
			"Project": modules["project"].get_project_permission_query,
			"Task": modules["task"].get_task_permission_query,
			"Work Package": modules["work_package"].get_work_package_permission_query,
			"Task Progress Entry": modules["task_progress_entry"].get_task_progress_entry_permission_query,
			"Stage Planning": modules["stage_planning"].get_stage_planning_permission_query,
		}
		for doctype, query in queries.items():
			with self.subTest(doctype=doctype):
				self.assertEqual(query("finance@example.com"), "")

	def test_missing_or_malformed_config_stays_scoped(self):
		frappe, modules = _load_permission_modules()
		frappe.get_roles = lambda user: ["Finance Oversight"]

		cases = {
			"missing": {},
			"string": {"project_company_roles": "Finance Oversight"},
			"mixed_list": {"project_company_roles": ["Finance Oversight", 7]},
			"set": {"project_company_roles": {"Finance Oversight"}},
		}
		for label, config in cases.items():
			with self.subTest(config=label):
				frappe.conf = config
				self.assertTrue(modules["project"]._is_scoped("finance@example.com"))
				self.assertIn("tabProject", modules["project"].get_project_permission_query("finance@example.com"))


if __name__ == "__main__":
	unittest.main()
