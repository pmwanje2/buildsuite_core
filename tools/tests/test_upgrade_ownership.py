"""Test the real upgrade hooks with database/API boundaries isolated."""

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
import frappe.permissions

from buildsuite_core import hooks, install
from buildsuite_core.api import boq_actuals
from buildsuite_core.permissions import setup
from buildsuite_core.utils import daily_hours, full_day, timesheet

ROOT = Path(__file__).resolve().parents[2]


class TestUpgradeOwnership(unittest.TestCase):
	def setUp(self):
		self.flags = frappe._dict()
		self.conf = frappe._dict()
		self.db = Mock()
		self.db.exists.return_value = True
		self.db.get_value.return_value = 1
		self.add = Mock()
		self.change = Mock()
		self.get_all = Mock(return_value=[])
		with_mock = patch.multiple(
			frappe, flags=self.flags, conf=self.conf, db=self.db, get_all=self.get_all,
			get_meta=Mock(), clear_cache=Mock(),
		)
		with_mock.start()
		self.addCleanup(with_mock.stop)
		with_perms = patch.multiple(
			frappe.permissions, add_permission=self.add, update_permission_property=self.change,
		)
		with_perms.start()
		self.addCleanup(with_perms.stop)

	def assert_no_permissions_written(self):
		self.db.delete.assert_not_called()
		self.add.assert_not_called()
		self.change.assert_not_called()

	def test_migration_and_patch_permissions_are_default_off(self):
		for flag in ("in_patch", "in_migrate"):
			for configured in (None, False, "true", 1):
				with self.subTest(flag=flag, configured=configured):
					self.flags.clear()
					self.flags[flag] = True
					self.conf.buildsuite_sync_permissions_on_migrate = configured
					self.db.reset_mock()
					self.add.reset_mock()
					self.change.reset_mock()
					setup._apply_role_perms("Material Request", {})
					setup.setup_mobile_permissions()
					install.ensure_full_day_permissions()
					self.assert_no_permissions_written()

	def test_permission_patches_do_not_widen_or_rewrite_records_by_default(self):
		self.flags.in_patch = True
		for name in (
			"grant_item_group_select", "grant_foreman_item_select_read", "grant_account_select_read",
			"grant_timesheet_workforce_perms", "seed_terms_permissions",
			"scope_petty_cash_expense_visibility", "restore_petty_cash_expense_owner_to_creator",
			"adopt_beneficiary_owner_for_petty_cash_expense", "grant_hr_manager_attendance_registers",
		):
			with self.subTest(patch=name):
				self.db.reset_mock()
				self.add.reset_mock()
				self.change.reset_mock()
				importlib.import_module(f"buildsuite_core.patches.{name}").execute()
				self.assert_no_permissions_written()

	def test_beneficiary_backfill_does_not_transfer_owner_without_permission_sync(self):
		self.flags.in_patch = True
		self.get_all.return_value = [
			frappe._dict(name="DRAFT", owner="entry-maker", raised_by="entry-maker", requested_by="holder")
		]
		from buildsuite_core.patches.adopt_beneficiary_owner_for_petty_cash_expense import execute

		execute()
		self.db.set_value.assert_not_called()

	def test_additive_permission_helper_is_protected_even_without_existing_custom_rows(self):
		self.flags.in_patch = True
		self.db.exists.side_effect = lambda doctype, *_: doctype != "Custom DocPerm"
		setup._grant_missing_role_perms("Lead", {"BuildSuite QS": {"read": 1}})
		self.assert_no_permissions_written()

	def test_legacy_mirror_cleanup_does_not_rewrite_a_tenant_read_grant(self):
		self.flags.in_patch = True
		self.get_all.side_effect = lambda dt, **_: (
			[frappe._dict(name="TENANT-READ-RULE", parent="Unit of Measure")] if dt == "Custom DocPerm" else []
		)
		from buildsuite_core.patches.strip_read_mirror_overreach import execute

		execute()
		self.db.set_value.assert_not_called()

	def test_permission_sync_remains_available_to_install_and_explicit_opt_in(self):
		for flag, enabled in (("in_install", None), ("in_migrate", True)):
			with self.subTest(flag=flag):
				self.flags.clear()
				self.flags[flag] = True
				self.conf.buildsuite_sync_permissions_on_migrate = enabled
				self.change.reset_mock()
				install.ensure_full_day_permissions()
				self.change.assert_any_call("Company", "System Manager", 1, "write", 1)

	def test_machinery_usage_patch_does_not_transition_existing_rows(self):
		self.flags.in_patch = True
		from buildsuite_core.patches.submit_existing_machinery_usage import execute

		execute()
		self.db.sql.assert_not_called()
		self.assert_no_permissions_written()

	def test_machinery_usage_save_is_still_editable_without_submit_cancel_or_amend(self):
		path = ROOT / "buildsuite_core/buildsuite_core/doctype/machinery_usage/machinery_usage.json"
		schema = json.loads(path.read_text())
		self.assertFalse(schema.get("is_submittable"))
		self.assertFalse(any(p.get(key) for p in schema["permissions"] for key in ("submit", "cancel", "amend")))

	def test_saved_machinery_log_still_reaches_boq_actuals(self):
		row = frappe._dict(
			name="LOG", machine="PLANT", quantity=2, rate=3000, fuel_cost=1500,
			cost_code_type="Group", cost_code_group="A", docstatus=0,
		)
		self.get_all.side_effect = lambda dt, filters, **_: (
			[row] if dt == "Machinery Usage" and filters.get("docstatus", 0) == 0 else []
		)
		frappe.get_meta.return_value = frappe._dict(is_submittable=0)
		self.assertEqual([7500], [r["amount"] for r in boq_actuals._machinery_entries("PROJECT")])


class TestNativeTimesheetOwnership(unittest.TestCase):
	def setUp(self):
		self.conf = frappe._dict()
		self.errors = Mock()
		self.get_doc = Mock(return_value=frappe._dict(custom_wage=0, custom_wage_for_overtime=0))
		context = patch.multiple(
			frappe, conf=self.conf, throw=self.errors, get_doc=self.get_doc, db=Mock(), get_all=Mock(return_value=[]),
		)
		context.start()
		self.addCleanup(context.stop)
		for module in (timesheet, daily_hours, full_day):
			translate = patch.object(module, "_", lambda text: text)
			translate.start()
			self.addCleanup(translate.stop)
		self.sheet = frappe._dict(
			docstatus=0, name="DW-1", employee="EMP", company="COMPANY", total_costing_amount=1250,
			custom_scope_change_order="CHANGE-1", get_doc_before_save=lambda: None,
			time_logs=[frappe._dict(
				name="ROW", from_time="2026-11-21 08:00:00", project="PROJECT",
				hours=10, costing_rate=125, costing_amount=1250,
			)],
		)

	def run_handlers(self, event):
		paths = hooks.doc_events["Timesheet"][event]
		for path in (paths if isinstance(paths, list) else [paths]):
			module, method = path.rsplit(".", 1)
			getattr(importlib.import_module(module), method)(self.sheet, event)

	def test_default_does_not_reprice_native_activity_cost(self):
		with patch.object(timesheet, "get_full_day_hours", return_value=8):
			timesheet.cost_timesheet(self.sheet)
		row = self.sheet.time_logs[0]
		self.assertEqual((125, 1250, 1250), (row.costing_rate, row.costing_amount, self.sheet.total_costing_amount))

	def test_default_leaves_separate_same_day_timesheets_to_erpnext(self):
		with patch.object(timesheet, "existing_timesheet_for_day", return_value="REGULAR-1") as existing:
			self.run_handlers("before_validate")
		self.errors.assert_not_called()
		existing.assert_not_called()

	def test_default_does_not_impose_labour_caps_on_native_daywork(self):
		with (
			patch.object(daily_hours, "get_full_day_hours", return_value=8),
			patch.object(daily_hours, "check_timesheet_day", return_value="daily labour cap") as limit,
		):
			daily_hours.validate_timesheet_hours(self.sheet)
		self.errors.assert_not_called()
		limit.assert_not_called()

	def test_default_submit_never_creates_registers_for_native_timesheets(self):
		for employee, project in (("", "PROJECT"), ("EMP", ""), ("EMP", "PROJECT")):
			with self.subTest(employee=employee, project=project):
				self.get_doc.reset_mock()
				self.sheet.employee = employee
				self.sheet.time_logs[0].project = project
				self.sheet.docstatus = 1
				self.get_doc.return_value = Mock()
				self.run_handlers("on_submit")
				self.get_doc.assert_not_called()

	def test_default_leaves_native_cancel_and_delete_alone(self):
		self.run_handlers("on_cancel")
		self.run_handlers("on_trash")
		frappe.get_all.assert_not_called()

	def test_new_project_default_float_does_not_require_manager_authority(self):
		non_manager = Mock(return_value=False)
		with patch.object(full_day, "can_edit_project_full_day", non_manager):
			with patch.object(frappe, "flags", frappe._dict()):
				full_day.validate_project_full_day_hours(frappe._dict(
					is_new=lambda: True, full_day_hours=0.0,
				))
		self.errors.assert_not_called()
		non_manager.assert_not_called()

	def test_nonzero_new_project_setting_still_requires_manager(self):
		with (
			patch.object(full_day, "can_edit_project_full_day", return_value=False),
			patch.object(frappe, "flags", frappe._dict()),
		):
			full_day.validate_project_full_day_hours(frappe._dict(is_new=lambda: True, full_day_hours=9))
		self.assertIs(self.errors.call_args.args[1], frappe.PermissionError)
