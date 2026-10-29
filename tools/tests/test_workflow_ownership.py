"""Offline regression of the real seeder; no live site or database is used."""

import ast
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = json.loads((Path(__file__).parent / "fixtures/default_workflows.json").read_text())


class Doc(types.SimpleNamespace):
	def append(self, field, row):
		getattr(self, field).append(Doc(**row))

	def set(self, field, value):
		setattr(self, field, value)

	def insert(self, **kwargs):
		if kwargs.get("ignore_links") or getattr(self.flags, "ignore_links", False):
			raise AssertionError("Role links must be validated")
		if getattr(self, "doctype", None) == "Role":
			if self.role_name in self.roles:
				raise AssertionError(f"Existing Role must not be inserted: {self.role_name}")
			self.roles.add(self.role_name)
			return self
		# Validate the moved fixture payloads at the same boundary as Document.insert.
		if getattr(self, "doctype", None) == "Workflow":
			for rows, field in ((self.states, "allow_edit"), (self.transitions, "allowed")):
				for row in rows:
					role = getattr(row, field)
					if role not in self.roles:
						raise AssertionError(f"LinkValidationError: Could not find Role: {role}")
		self.name = self.workflow_name
		self.store[self.name] = self
		return self

	def save(self, **_kwargs):
		self.saved = True

	def as_dict(self):
		return {
			key: [row.as_dict() for row in value] if isinstance(value, list) else value
			for key, value in vars(self).items()
			if key not in ("store", "flags", "roles")
		}


def document(payload, store, roles=None):
	if payload.get("doctype") == "Role":
		assert set(payload) <= {"doctype", "role_name", "desk_access"}, "Do not grant permissions"
	values = copy.deepcopy(payload)
	for key in ("states", "transitions"):
		values[key] = [Doc(**row) for row in values.get(key, [])]
	return Doc(**values, store=store, roles=roles, flags=types.SimpleNamespace())


class WorkflowOwnershipTests(unittest.TestCase):
	def setUp(self):
		self.store = {}
		self.roles = {"BuildSuite Administrator"}
		self.frappe = types.ModuleType("frappe")
		self.frappe.db = types.SimpleNamespace(exists=self.exists)
		self.frappe.get_doc = lambda dt, name=None: (
			document(dt, self.store, self.roles) if isinstance(dt, dict) else self.store[name]
		)
		self.frappe.new_doc = lambda _dt: document({}, self.store)
		self.modules = patch.dict(sys.modules, {"frappe": self.frappe})
		self.modules.start()
		self.addCleanup(self.modules.stop)
		self.seed = self.load("buildsuite_core/default_workflows.py", "ownership_seed")
		self.setup = self.load("buildsuite_core/permissions/setup.py", "ownership_setup")

	def load(self, path, name):
		spec = importlib.util.spec_from_file_location(name, ROOT / path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module

	def exists(self, doctype, filters):
		if doctype == "Role":
			return filters in self.roles
		if doctype != "Workflow":
			return True  # Shared workflow masters already exist.
		if isinstance(filters, str):
			return filters in self.store
		return any(
			all(getattr(wf, key) == value for key, value in filters.items()) for wf in self.store.values()
		)

	def test_migrate_directory_has_no_workflow_records(self):
		self.assertFalse((ROOT / "buildsuite_core/fixtures/workflow.json").exists())
		for path in (ROOT / "buildsuite_core/fixtures").glob("*.json"):
			self.assertFalse(
				any(row.get("doctype") == "Workflow" for row in json.loads(path.read_text())), path
			)

	def test_export_cannot_recreate_workflow_fixture(self):
		tree = ast.parse((ROOT / "buildsuite_core/hooks.py").read_text())
		fixtures = next(
			ast.literal_eval(node.value)
			for node in tree.body
			if isinstance(node, ast.Assign)
			and any(isinstance(t, ast.Name) and t.id == "fixtures" for t in node.targets)
		)
		self.assertNotIn("Workflow", [row if isinstance(row, str) else row["doctype"] for row in fixtures])

	def test_fresh_install_preserves_three_defaults_with_material_request_opt_in(self):
		self.assertEqual({"BuildSuite Administrator"}, self.roles)
		created = self.seed.seed_default_workflows()
		for expected in DEFAULTS:
			with self.subTest(doctype=expected["document_type"]):
				self.assertIn(expected["name"], created)
				wf = self.store[expected["name"]].as_dict()
				for key, value in expected.items():
					want = (
						0 if key == "is_active" and expected["document_type"] == "Material Request" else value
					)
					self.assertEqual(want, wf[key], key)
				for rows, field in ((wf["states"], "allow_edit"), (wf["transitions"], "allowed")):
					for row in rows:
						self.assertIn(row[field], self.roles)
		self.assertFalse(any(
			wf.is_active for wf in self.store.values() if wf.document_type == "Material Request"
		))
		# Existing finance defaults remain opt-in, not silently enabled.
		self.assertEqual(0, self.store["Purchase Invoice Approval"].is_active)

	def test_existing_named_defaults_are_never_modified(self):
		for expected in DEFAULTS:
			wf = document(expected, self.store)
			wf.is_active = 0
			wf.transitions[0].condition = "doc.owner != frappe.session.user"
			self.store[wf.name] = wf
		before = {name: wf.as_dict() for name, wf in self.store.items()}
		self.seed.seed_default_workflows()
		self.assertEqual({"BuildSuite Administrator"}, self.roles)
		for name, value in before.items():
			self.assertEqual(value, self.store[name].as_dict())

	def test_any_existing_workflow_blocks_vendor_activation_even_if_inactive(self):
		for expected in DEFAULTS:
			wf = document(expected, self.store)
			wf.name = wf.workflow_name = "Tenant " + expected["document_type"]
			wf.is_active = 0
			self.store[wf.name] = wf
		created = self.seed.seed_default_workflows()
		self.assertEqual({"BuildSuite Administrator"}, self.roles)
		for expected in DEFAULTS:
			self.assertNotIn(expected["name"], created)
			self.assertNotIn(expected["name"], self.store)
		self.assertEqual([], self.seed.seed_default_workflows())

	def test_existing_roles_are_reused_and_repeat_seed_is_a_no_op(self):
		self.roles.update(
			row[field]
			for default in DEFAULTS
			for rows, field in ((default["states"], "allow_edit"), (default["transitions"], "allowed"))
			for row in rows
		)
		before_roles = self.roles.copy()
		self.seed.seed_default_workflows()
		before = {name: wf.as_dict() for name, wf in self.store.items()}
		self.assertEqual([], self.seed.seed_default_workflows())
		self.assertEqual(before_roles, self.roles)
		self.assertEqual(before, {name: wf.as_dict() for name, wf in self.store.items()})

	def test_restore_permissions_does_not_rewrite_customer_stage_workflow(self):
		original = next(row for row in DEFAULTS if row["document_type"] == "Stage Planning")
		wf = document(original, self.store)
		wf.states[0].allow_edit = "Site Engineer SCL"
		wf.transitions[0].allowed = "Project Manager SCL"
		wf.transitions[0].condition = "doc.owner != frappe.session.user"
		wf.transitions[0].allow_self_approval = 0
		self.store[wf.name] = wf
		before = wf.as_dict()
		self.setup.setup_stage_planning_workflow()
		self.assertEqual(before, wf.as_dict())

	def test_restore_permissions_still_accepts_unmodified_vendor_default(self):
		original = next(row for row in DEFAULTS if row["document_type"] == "Stage Planning")
		wf = document(original, self.store)
		self.store[wf.name] = wf
		self.setup.setup_stage_planning_workflow()
		self.assertTrue(wf.saved)
		self.assertEqual(original["transitions"], [row.as_dict() for row in wf.transitions])


if __name__ == "__main__":
	unittest.main()
