# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""Restore native Template status on Tasks marked as templates.

Status is written directly so Task validate hooks do not overwrite it again.
Modified is left unchanged.
"""

import frappe


def execute():
	for name in frappe.get_all(
		"Task",
		filters={"is_template": 1, "status": ["!=", "Template"]},
		pluck="name",
	):
		frappe.db.set_value("Task", name, "status", "Template", update_modified=False)
