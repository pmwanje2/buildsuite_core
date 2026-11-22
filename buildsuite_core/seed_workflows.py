# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""Seed the shipped approval Workflows (Stage Planning, Scope Change Order, Material Request).

They used to be fixtures, which Frappe re-imports over the site's copy on every migrate, silently
discarding an admin's edits. They are now created only when missing: a workflow that already exists
is never touched, so changes made in Desk survive. Delivering a changed definition to existing sites
is a patch's job. The definitions live in data/workflows.json."""

import json
from pathlib import Path

import frappe

_DATA = Path(__file__).parent / "data" / "workflows.json"


def _roles_of(definition) -> set[str]:
	"""Every role a workflow definition names: who may act on a transition, who may edit in a state."""
	roles = {t.get("allowed") for t in definition.get("transitions", [])}
	roles |= {s.get("allow_edit") for s in definition.get("states", [])}
	return {r for r in roles if r}


def workflow_definitions():
	return json.loads(_DATA.read_text())


def seed_workflows():
	from buildsuite_core.permissions.setup import _ensure_role

	for definition in workflow_definitions():
		doctype = definition["document_type"]
		if frappe.db.exists("Workflow", definition["workflow_name"]) or frappe.db.exists(
			"Workflow", {"document_type": doctype}
		):
			continue
		if not frappe.db.exists("DocType", doctype):
			continue
		# The tenant's release strategy owns Material Request approval.
		if doctype == "Material Request":
			definition["is_active"] = 0
		# A workflow row links to a Role and Frappe checks the link on insert. On a fresh install this
		# runs before the BuildSuite roles are created (permissions.setup does that, later in
		# after_install), so create any role the definition needs first; that setup finds them in place.
		for role in sorted(_roles_of(definition)):
			_ensure_role(role)
		frappe.get_doc(definition).insert(ignore_permissions=True)
