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


def _planner(
	config=None,
	*,
	member=True,
	saved_state="Draft",
	saved_owner=USER,
	saved_project="PROJECT-001",
	roles=(PLANNER, "Quantity Surveyor SCL", "Employee"),
):
	"""Stub the persisted row: the hooks must authorize what is saved, not what is posted."""
	frappe, modules = _load_permission_modules()
	if config is not None:
		frappe.conf["project_planner_roles"] = config
	frappe.get_roles = lambda user: list(roles)
	teamed = {"PROJECT-001"} if member else set()
	frappe.db.exists.side_effect = lambda doctype, filters=None: (filters or {}).get("parent") in teamed
	saved = {"workflow_state": saved_state, "owner": saved_owner, "project": saved_project}
	frappe.db.get_value.side_effect = lambda doctype, name, field, *a, **k: saved.get(field)
	return frappe, modules


def _stage(owner=USER, state="Draft"):
	return _StubDocument(name="STG-1", project="PROJECT-001", owner=owner, workflow_state=state)


def _task(owner="pm@example.com", project="PROJECT-001"):
	return _StubDocument(name="TASK-1", project=project, owner=owner)


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

	def test_forged_owner_cannot_take_over_another_users_stage_plan(self):
		# Review finding: a teamed planner posts a colleague's Draft with owner rewritten to self.
		_, modules = _planner([PLANNER], saved_owner="other@example.com")
		check = modules["stage_planning"].has_stage_planning_permission
		self.assertFalse(check(_stage(owner=USER), ptype="write", user=USER))

	def test_forged_owner_is_refused_for_site_engineer_and_foreman_too(self):
		for role in ("BuildSuite Site Engineer", "BuildSuite Foreman"):
			with self.subTest(role=role):
				_, modules = _planner(None, saved_owner="other@example.com", roles=(role,))
				check = modules["stage_planning"].has_stage_planning_permission
				self.assertFalse(check(_stage(owner=USER), ptype="write", user=USER))

	def test_planner_task_write_authorizes_the_saved_project(self):
		# Posted project is teamed, saved project is not: the saved row decides.
		_, modules = _planner([PLANNER], saved_project="PROJECT-OTHER")
		self.assertFalse(modules["task"].has_task_permission(_task(), ptype="write", user=USER))

	def test_planner_task_write_refused_when_posted_project_is_cleared(self):
		for posted in ("", None):
			with self.subTest(posted=posted):
				_, modules = _planner([PLANNER])
				self.assertFalse(
					modules["task"].has_task_permission(_task(project=posted), ptype="write", user=USER)
				)

	def test_planner_task_write_refused_for_task_without_project(self):
		_, modules = _planner([PLANNER], saved_project=None)
		self.assertFalse(modules["task"].has_task_permission(_task(project=None), ptype="write", user=USER))


if __name__ == "__main__":
	unittest.main()
