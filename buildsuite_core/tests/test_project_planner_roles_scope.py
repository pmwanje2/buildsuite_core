"""Configured project planner roles (SAP PS scheduler) under BuildSuite team scoping.

SAP PS: the scheduler maintains the activities and dates of the projects they plan and prepares
their own stage plan; release (approval) stays with the project manager. Site config
`project_planner_roles` names the tenant roles that act as scheduler. Absent or invalid config keeps
today's behaviour (fail closed). Offline: frappe is stubbed, no site or database is used.
"""

from __future__ import annotations

import unittest

from buildsuite_core.tests.test_project_company_roles_scope import _load_permission_modules, _StubDocument

PLANNER = "Project Planner SCL"
USER = "planner@example.com"


def _planner(config=None, *, member=True, saved_state="Draft"):
	frappe, modules = _load_permission_modules()
	if config is not None:
		frappe.conf["project_planner_roles"] = config
	frappe.get_roles = lambda user: [PLANNER, "Quantity Surveyor SCL", "Employee"]
	frappe.db.exists.return_value = member
	frappe.db.get_value.return_value = saved_state
	return frappe, modules


def _stage(owner=USER, state="Draft"):
	return _StubDocument(name="STG-1", project="PROJECT-001", owner=owner, workflow_state=state)


def _task(owner="pm@example.com"):
	return _StubDocument(name="TASK-1", project="PROJECT-001", owner=owner)


class TestProjectPlannerRolesScope(unittest.TestCase):
	def test_planner_owner_can_write_own_draft_stage_plan(self):
		_, modules = _planner([PLANNER])
		check = modules["stage_planning"].has_stage_planning_permission
		self.assertTrue(check(_stage(), ptype="write", user=USER))

	def test_planner_owner_can_rework_own_rejected_stage_plan(self):
		_, modules = _planner([PLANNER], saved_state="Rejected")
		check = modules["stage_planning"].has_stage_planning_permission
		self.assertTrue(check(_stage(state="Rejected"), ptype="write", user=USER))

	def test_planner_cannot_write_stage_plan_after_submission(self):
		for state in ("Pending Approval", "Approved", "Cancelled"):
			with self.subTest(state=state):
				_, modules = _planner([PLANNER], saved_state=state)
				check = modules["stage_planning"].has_stage_planning_permission
				self.assertFalse(check(_stage(state=state), ptype="write", user=USER))

	def test_planner_cannot_write_another_users_stage_plan(self):
		_, modules = _planner([PLANNER])
		check = modules["stage_planning"].has_stage_planning_permission
		self.assertFalse(check(_stage(owner="other@example.com"), ptype="write", user=USER))

	def test_planner_outside_project_team_is_refused(self):
		_, modules = _planner([PLANNER], member=False)
		self.assertFalse(
			modules["stage_planning"].has_stage_planning_permission(_stage(), ptype="write", user=USER)
		)
		self.assertFalse(modules["task"].has_task_permission(_task(), ptype="write", user=USER))

	def test_planner_can_maintain_any_task_of_a_teamed_project(self):
		_, modules = _planner([PLANNER])
		self.assertTrue(modules["task"].has_task_permission(_task(), ptype="write", user=USER))

	def test_planner_never_deletes_tasks(self):
		_, modules = _planner([PLANNER])
		self.assertFalse(modules["task"].has_task_permission(_task(), ptype="delete", user=USER))

	def test_absent_or_invalid_config_keeps_todays_behaviour(self):
		for config in (None, [], "Project Planner SCL", [PLANNER, 3]):
			with self.subTest(config=config):
				_, modules = _planner(config)
				self.assertFalse(
					modules["stage_planning"].has_stage_planning_permission(
						_stage(), ptype="write", user=USER
					)
				)
				self.assertFalse(modules["task"].has_task_permission(_task(), ptype="write", user=USER))

	def test_unlisted_role_gains_nothing(self):
		_, modules = _planner(["Some Other Planner"])
		self.assertFalse(
			modules["stage_planning"].has_stage_planning_permission(_stage(), ptype="write", user=USER)
		)
		self.assertFalse(modules["task"].has_task_permission(_task(), ptype="write", user=USER))


if __name__ == "__main__":
	unittest.main()
