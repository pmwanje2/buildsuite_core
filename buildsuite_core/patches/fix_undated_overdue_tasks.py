# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""Re-save Tasks incorrectly marked Overdue without an expected end date.

Saving through the document lifecycle lets the Task hooks recalculate native
status and records the repair in the normal document history.
"""

import frappe


def execute():
	for name in frappe.get_all(
		"Task",
		filters={"status": "Overdue", "exp_end_date": ["is", "not set"]},
		pluck="name",
		order_by="name asc",
		limit_page_length=500,
	):
		try:
			frappe.get_doc("Task", name).save(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				f"Failed to repair overdue Task {name}",
				"BuildSuite: undated overdue Task repair",
			)
