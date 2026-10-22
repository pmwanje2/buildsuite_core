"""Company-wide project roles may bypass BuildSuite team scoping."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


class _StubDocument(dict):
	__getattr__ = dict.get


def _load_permission_modules():
	frappe = types.ModuleType("frappe")
	frappe.__path__ = []
	frappe.conf = {}
	frappe.get_roles = lambda user: []
	frappe.session = types.SimpleNamespace(user="test@example.com")
	frappe.db = types.SimpleNamespace(
		exists=Mock(return_value=False),
		escape=lambda value: f"'{value}'",
		get_value=Mock(return_value=None),
	)
	frappe_utils = types.ModuleType("frappe.utils")
	frappe_utils.get_datetime = lambda value: value
	frappe_utils.now_datetime = lambda: datetime(2026, 1, 2)

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
	def test_finance_oversight_role_stays_scoped_for_writes_but_can_read(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Finance Oversight"]
		frappe.get_roles = lambda user: ["Finance Oversight"]

		self.assertTrue(modules["project"]._is_scoped("finance@example.com"))
		self.assertEqual(modules["project"].get_project_permission_query("finance@example.com"), "")
		self.assertTrue(
			modules["project"].has_project_permission(
				types.SimpleNamespace(name="PROJECT-001"), user="finance@example.com"
			)
		)
		self.assertFalse(
			modules["project"].has_project_permission(
				types.SimpleNamespace(name="PROJECT-001"), ptype="write", user="finance@example.com"
			)
		)

	def test_teamed_accountant_listed_in_config_is_unscoped(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["BuildSuite Accountant"]
		frappe.get_roles = lambda user: ["BuildSuite Accountant"]
		frappe.db.exists.return_value = True

		self.assertTrue(modules["project"]._is_scoped("accountant@example.com"))
		self.assertEqual(modules["project"].get_project_permission_query("accountant@example.com"), "")

	def test_company_site_engineer_cannot_modify_outside_team_or_other_users_task(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Stock Manager"]
		frappe.get_roles = lambda user: ["Stock Manager", "BuildSuite Site Engineer"]
		task = _StubDocument(name="TASK-OTHER", project="PROJECT-OTHER", owner="other@example.com")

		frappe.db.exists.return_value = False
		self.assertFalse(
			modules["task"].has_task_permission(task, ptype="create", user="site@example.com")
		)
		for ptype in ("write", "delete"):
			with self.subTest(ptype=ptype):
				self.assertFalse(
					modules["task"].has_task_permission(task, ptype=ptype, user="site@example.com")
				)

		frappe.db.exists.return_value = True
		self.assertFalse(
			modules["task"].has_task_permission(task, ptype="write", user="site@example.com")
		)
		self.assertFalse(
			modules["task"].has_task_permission(task, ptype="delete", user="site@example.com")
		)

	def test_company_site_engineer_cannot_edit_approved_stage_planning(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Stock Manager"]
		frappe.get_roles = lambda user: ["Stock Manager", "BuildSuite Site Engineer"]
		frappe.db.exists.return_value = True
		frappe.db.get_value.return_value = "Approved"
		stage = _StubDocument(
			name="STAGE-OTHER",
			project="PROJECT-001",
			owner="other@example.com",
			workflow_state="Approved",
		)

		self.assertFalse(
			modules["stage_planning"].has_stage_planning_permission(
				stage, ptype="write", user="site@example.com"
			)
		)

	def test_company_site_engineer_cannot_delete_other_users_progress_entry_or_old_own_entry(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Stock Manager"]
		frappe.get_roles = lambda user: ["Stock Manager", "BuildSuite Site Engineer"]
		frappe.db.exists.return_value = True
		frappe.db.get_value.return_value = "PROJECT-001"

		other_entry = _StubDocument(
			name="TPE-OTHER",
			task="TASK-001",
			owner="other@example.com",
			creation=datetime(2026, 1, 2),
		)
		self.assertFalse(
			modules["task_progress_entry"].has_task_progress_entry_permission(
				other_entry, ptype="delete", user="site@example.com"
			)
		)

		old_own_entry = _StubDocument(
			name="TPE-OWN",
			task="TASK-001",
			owner="site@example.com",
			creation=datetime(2025, 12, 31),
		)
		self.assertFalse(
			modules["task_progress_entry"].has_task_progress_entry_permission(
				old_own_entry, ptype="delete", user="site@example.com"
			)
		)

	def test_company_site_engineer_can_read_and_list_all_projects_and_tasks(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = ["Stock Manager"]
		frappe.get_roles = lambda user: ["Stock Manager", "BuildSuite Site Engineer"]
		project = _StubDocument(name="PROJECT-OTHER")
		task = _StubDocument(name="TASK-OTHER", project="PROJECT-OTHER", owner="other@example.com")

		self.assertEqual(modules["project"].get_project_permission_query("site@example.com"), "")
		self.assertEqual(modules["task"].get_task_permission_query("site@example.com"), "")
		for ptype in ("read", "select", "report", "print", "export"):
			with self.subTest(ptype=ptype):
				self.assertTrue(
					modules["project"].has_project_permission(
						project, ptype=ptype, user="site@example.com"
					)
				)
				self.assertTrue(
					modules["task"].has_task_permission(task, ptype=ptype, user="site@example.com")
				)

	def test_empty_company_role_list_fails_closed_to_team_scope(self):
		frappe, modules = _load_permission_modules()
		frappe.conf["project_company_roles"] = []
		frappe.get_roles = lambda user: ["BuildSuite Site Engineer"]
		frappe.db.exists.return_value = False

		self.assertTrue(modules["project"]._is_scoped("site@example.com"))
		self.assertIn("tabProject", modules["project"].get_project_permission_query("site@example.com"))
		self.assertFalse(
			modules["task"].has_task_permission(
				_StubDocument(project="PROJECT-OTHER"), ptype="create", user="site@example.com"
			)
		)

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
