"""Tenant workflow ownership at the upstream seeder and Stage Planning setup boundaries."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TestWorkflowOwnership(unittest.TestCase):
	def setUp(self):
		self.workflows = {}
		self.roles = set()
		frappe = SimpleNamespace(
			db=SimpleNamespace(exists=self.exists),
			get_doc=self.get_doc,
		)
		with patch.dict(sys.modules, {"frappe": frappe}):
			from buildsuite_core import seed_workflows
			from buildsuite_core.permissions import setup

		modules = patch.dict(
			sys.modules,
			{"buildsuite_core.permissions.setup": setup, "buildsuite_core.seed_workflows": seed_workflows},
		)
		modules.start()
		self.addCleanup(modules.stop)
		for module in (seed_workflows, setup):
			patcher = patch.object(module, "frappe", frappe)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.seed_workflows = seed_workflows.seed_workflows
		self.setup_stage_workflow = setup.setup_stage_planning_workflow

	def exists(self, doctype, value):
		if doctype == "DocType":
			return True
		if doctype == "Role":
			return value in self.roles
		if isinstance(value, dict):
			return any(row["document_type"] == value["document_type"] for row in self.workflows.values())
		return value in self.workflows

	def get_doc(self, value, name=None):
		if value == "Workflow":
			return self.workflows[name]["document"]
		if value["doctype"] == "Role":
			return SimpleNamespace(insert=lambda **_: self.roles.add(value["role_name"]))

		def insert(**_):
			for child_table, field in (("states", "allow_edit"), ("transitions", "allowed")):
				for row in value[child_table]:
					self.assertIn(row[field], self.roles)
			self.workflows[value["workflow_name"]] = value

		return SimpleNamespace(insert=insert)

	def test_does_not_seed_over_a_differently_named_tenant_workflow(self):
		tenant = {"document_type": "Scope Change Order", "is_active": 1}
		self.workflows["Tenant SCO approval"] = tenant
		self.seed_workflows()
		self.assertEqual(tenant, self.workflows["Tenant SCO approval"])
		self.assertNotIn("Scope Change Order Approval", list(self.workflows))

	def test_material_request_default_is_opt_in(self):
		self.seed_workflows()
		self.assertEqual(0, self.workflows["Material Request Approval"]["is_active"])
		self.assertEqual(1, self.workflows["Stage Planning Approval"]["is_active"])

	def test_stage_permission_setup_preserves_tenant_customization(self):
		self.seed_workflows()
		stage = self.workflows["Stage Planning Approval"]
		stage["transitions"][0]["condition"] = "doc.owner != frappe.session.user"
		document = Mock()
		document.as_dict.return_value = stage
		document.states = [SimpleNamespace(**state) for state in stage["states"]]
		stage["document"] = document

		self.setup_stage_workflow()

		document.save.assert_not_called()
		self.assertEqual([state["allow_edit"] for state in stage["states"]], [
			state.allow_edit for state in document.states
		])

	def test_stage_permission_setup_still_rewires_untouched_vendor_default(self):
		self.seed_workflows()
		stage = self.workflows["Stage Planning Approval"]
		document = Mock()
		document.as_dict.return_value = stage
		document.states = [SimpleNamespace(**state) for state in stage["states"]]
		stage["document"] = document

		self.setup_stage_workflow()

		document.save.assert_called_once_with(ignore_permissions=True)
