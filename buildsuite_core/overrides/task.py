# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""Task controller override.

ERPNext's Task.set_default_end_date_if_missing() auto-fills exp_end_date from
exp_start_date + expected_time (hours) whenever the end is empty. Tasks seeded from
a template carry expected_time, so editing one and saving with a start but no end
would fabricate a "random" end date — while a freshly-created task (no expected_time)
correctly keeps its end empty. BuildSuite wants the end date to stay empty unless the
user enters one, regardless of how the task was created, so we no-op that auto-fill.
"""

import frappe
from erpnext.projects.doctype.task.task import Task as _ERPNextTask


class BuildSuiteTask(_ERPNextTask):
	def set_default_end_date_if_missing(self):
		# Intentionally a no-op — never derive the end date from expected_time.
		pass

	def reschedule_dependent_tasks(self):
		if (
			getattr(self.flags, "ignore_reschedule", False)
			or frappe.flags.get("ignore_reschedule")
			or frappe.flags.get("in_schedule_cascade")
			or not (self.has_value_changed("exp_start_date") or self.has_value_changed("exp_end_date"))
		):
			return

		from buildsuite_core.api.schedule_engine import (
			_apply_moves,
			_load_graph,
			compute_cascade,
		)

		tasks_by_id, edges = _load_graph(self.project)
		if self.name not in tasks_by_id:
			return
		moves = compute_cascade(
			self.name,
			tasks_by_id,
			edges,
			{"start": self.exp_start_date, "end": self.exp_end_date},
		)
		if moves is None:
			frappe.throw("Cycle detected in the downstream schedule — cascade aborted.")
		_apply_moves(self, moves)
