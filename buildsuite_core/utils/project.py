import frappe
from frappe import _
from frappe.model.document import Document

# BuildSuite project_status -> native ERPNext Project status (Open/Completed/Cancelled).
# New/Ongoing/Delayed all map to the active "Open"; only Completed closes the project.
_PROJECT_STATUS_TO_NATIVE = {
	"New": "Open",
	"Ongoing": "Open",
	"Delayed": "Open",
	"Completed": "Completed",
}


def sync_project_status(doc, method=None):
	"""Keep native `status` aligned with the BuildSuite `project_status` enum.

	project_status (New/Ongoing/Delayed/Completed) is the source of truth shown in
	the UI; the native status only carries Open/Completed/Cancelled, so we map onto
	it. Defaults a blank project_status to New.
	"""
	if not getattr(doc, "project_status", None):
		doc.project_status = "New"
	doc.status = _PROJECT_STATUS_TO_NATIVE.get(doc.project_status, "Open")

	# Progress is ALWAYS the weighted task rollup — fully decoupled from status.
	# erpnext's update_percent_complete (runs earlier in validate) otherwise (a) forces
	# percent to 100 when a project leaves the "Completed" status, and (b) re-derives
	# status from percent. Pin the method to Manual to silence its auto-calc, and on an
	# existing project recompute percent from our rollup here (after erpnext's pass) so
	# a status change never overwrites progress and 100% happens only when tasks do.
	doc.percent_complete_method = "Manual"
	if not doc.is_new():
		from buildsuite_core.utils.task import compute_project_progress

		doc.percent_complete = compute_project_progress(doc.name)


def default_company():
	"""The company a new record defaults to (and the backend scope resolves to) — the creating
	user's company, else their user default, else the site default. This is the resolution the New
	Project screen uses; shared so other docs (e.g. a direct Subcontractor Bill with no project)
	default their accounting company the same way.

	When company awareness is ON, the topbar switcher's explicit choice wins, so the backend scope
	matches the client switcher. That choice is the user's personal Company default (set by
	set_active_company) — read from the DefaultValue row directly, because
	frappe.defaults.get_user_default("Company") returns the User.company FIELD for the Company key,
	not the personal default, and would otherwise ignore the switch for users who have a company on
	their User record."""
	user = frappe.session.user
	if is_multi_company_enabled():
		# The user's personal Company default (parent = the user, parenttype "__default"), set by
		# set_active_company. defkey compares case-insensitively, so "company" matches the stored
		# "Company". Empty when the user hasn't switched — then fall through to the usual chain.
		chosen = frappe.db.get_value(
			"DefaultValue",
			{"parent": user, "defkey": "company"},
			"defvalue",
		)
		if chosen:
			return chosen
	return (
		frappe.db.get_value("User", user, "company")
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)


def is_multi_company_enabled():
	"""Whether company awareness is turned on (BuildSuite Core Settings). Off (default) keeps the
	app single-company: the topbar switcher is hidden and lists/pickers are not company-scoped."""
	return bool(frappe.db.get_single_value("BuildSuite Core Settings", "multi_company_enabled"))


def company_scope():
	"""The company to scope company-aware LISTS and PICKERS to: the working company when
	awareness is enabled, else None (no scoping — show all). This is only for filtering; new-record
	company defaults and the cross-company guards always use default_company()/the project company,
	so a single-company site keeps a valid company on every document regardless of this flag."""
	return default_company() if is_multi_company_enabled() else None


def stamp_company_on_insert(doc, method=None):
	"""Stamp the working company on an org-wide ERPNext master (Supplier / Customer / Item) at
	insert so it can be company-scoped. Only sets when blank — never overrides an explicit
	choice. Wired via hooks.py doc_events for those doctypes.
	"""
	if not doc.get("company"):
		doc.company = default_company()


def anchor_company_to_project(doc, project_field="project"):
	"""Anchor a document's company to its project's company (the accounting-company rule).

	Always re-derives `doc.company` from the linked project — never just when blank — so a stale
	value or the user's default company can't drift away from the project. Throws if the project
	has no company. With no project yet, falls back to default_company(). This is the reusable
	form of SubcontractorWorkOrder._set_company / SubcontractorBill._sync_from_work_order — call
	it from a project-scoped doctype's validate().
	"""
	project = doc.get(project_field)
	if project:
		project_company = frappe.db.get_value("Project", project, "company")
		# Fall back to the default company when the project has none yet (e.g. it was created
		# before a default company was configured). A hard throw here would block the save AND
		# silently abort template seeding (the WP/Stage inserts are swallowed by
		# seed_from_template_on_insert's try/except). Downstream same-company guards no-op on a
		# blank company, so a company-less project degrades safely rather than failing loud.
		doc.company = project_company or default_company()
	elif not doc.get("company"):
		doc.company = default_company()


def assert_same_company(doc, link_field, link_doctype, label=None):
	"""Guard that a linked, company-scoped record shares `doc`'s company.

	Blocks cross-company mixing — e.g. a Company A BOQ referenced by a Company B scope-change
	order. No-op when either side has no company yet (anchor the document's company first).
	`label` is the human name used in the error (defaults to the link doctype).
	"""
	link_name = doc.get(link_field)
	if not link_name or not doc.get("company"):
		return
	other = frappe.db.get_value(link_doctype, link_name, "company")
	if other and other != doc.get("company"):
		frappe.throw(
			frappe._("{0} {1} belongs to company {2}, not {3} — it cannot be used here.").format(
				label or link_doctype, link_name, other, doc.get("company")
			),
			title=frappe._("Company mismatch"),
		)


def assert_link_same_company(link_name, link_doctype, company, label=None):
	"""The linked record must belong to `company` — used to keep a company-scoped master (e.g. a
	Supplier / Customer on a bill or invoice) from being pulled across companies. No-op when either
	side is unset.
	"""
	if not link_name or not company:
		return
	other = frappe.db.get_value(link_doctype, link_name, "company")
	if other and other != company:
		frappe.throw(
			frappe._("{0} {1} belongs to company {2}, not {3} — it cannot be used here.").format(
				label or link_doctype, link_name, other, company
			),
			title=frappe._("Company mismatch"),
		)


def set_company_on_insert(doc, method=None):
	"""Default/inherit company before insert (PRJ-005, PRJ-012).

	Subprojects inherit the parent's company; top-level projects fall back to the
	user's default company when none was chosen (covers single-company sites where
	the field is hidden in the UI).
	"""
	if doc.get("parent_project"):
		parent_company = frappe.db.get_value("Project", doc.parent_project, "company")
		if parent_company:
			doc.company = parent_company
			return

	if not doc.get("company"):
		doc.company = default_company()


def enforce_company_rules(doc, method=None):
	"""Lock company after create and keep subproject company inherited (PRJ-005, PRJ-013)."""
	# Subprojects always track their parent's company.
	if doc.get("parent_project"):
		parent_company = frappe.db.get_value("Project", doc.parent_project, "company")
		if parent_company:
			doc.company = parent_company

	# Company is read-only after create: revert any attempt to change it on edit.
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before and before.company and doc.company != before.company:
			doc.company = before.company


# Accounting / stock DocTypes that, if they reference a project, must block its
# deletion. Each entry: (doctype, fieldname holding the project link).
_FINANCIAL_LINKS = [
	("GL Entry", "project"),
	("Journal Entry Account", "project"),
	("Sales Invoice", "project"),
	("Purchase Invoice", "project"),
	("Stock Entry", "project"),
	("Payment Entry", "project"),
]


def _projects_in_tree(project):
	"""All project names in the subtree rooted at `project` (inclusive)."""
	names = [project]
	children = frappe.get_all("Project", filters={"parent_project": project}, pluck="name")
	for child in children:
		names.extend(_projects_in_tree(child))
	return names


def _assert_no_financial_links(project_names):
	"""Block deletion when accounting/stock documents reference the project (or a
	subproject), and name each linked document type with its count so the user knows
	exactly what to remove or reassign first."""
	counts = {}  # document type -> number of linked records
	for doctype, field in _FINANCIAL_LINKS:
		if not frappe.db.exists("DocType", doctype):
			continue
		meta = frappe.get_meta(doctype)
		if not meta.has_field(field):
			continue
		if meta.istable:
			# Child table (e.g. Journal Entry Account) — report the parent document
			# (Journal Entry), counting each parent once.
			rows = frappe.get_all(
				doctype, filters={field: ("in", project_names)}, fields=["parenttype", "parent"]
			)
			for ptype, _parent in {(r.parenttype, r.parent) for r in rows}:
				counts[ptype] = counts.get(ptype, 0) + 1
		else:
			n = frappe.db.count(doctype, {field: ("in", project_names)})
			if n:
				counts[doctype] = counts.get(doctype, 0) + n

	if not counts:
		return

	# GL Entry is a ledger derivative of the source documents — drop it from the
	# message when a real document already explains the block.
	if len(counts) > 1:
		counts.pop("GL Entry", None)

	linked = ", ".join(f"{dt} ({n})" for dt, n in counts.items())
	frappe.throw(
		_(
			"Cannot delete this project — it has linked records in {0}. Remove or reassign these first."
		).format(linked)
	)


def cascade_delete_project(doc, method=None):
	"""Guarded cascade delete (PRJ-014, STG-011).

	Blocks if any accounting/stock entry references the project or its subprojects;
	otherwise removes subprojects, work packages, tasks, TPEs, stages, and
	attachments. Task deletion fires its own cascade (TPEs/attachments).
	"""
	project_names = _projects_in_tree(doc.name)
	_assert_no_financial_links(project_names)

	# Suppress progress rollup while we tear the tree down (tasks deleting here
	# would otherwise recompute ancestors that are about to be deleted too).
	previous = frappe.flags.get("buildsuite_cascading")
	frappe.flags.buildsuite_cascading = True
	try:
		# Delete descendants depth-first: child projects first (each re-enters this hook).
		for child in frappe.get_all("Project", filters={"parent_project": doc.name}, pluck="name"):
			frappe.delete_doc("Project", child, ignore_permissions=True, force=True)

		# Stage Plannings for this project.
		for stage in frappe.get_all("Stage Planning", filters={"project": doc.name}, pluck="name"):
			frappe.delete_doc("Stage Planning", stage, ignore_permissions=True, force=True)

		# Tasks (their on_trash cascades TPEs + attachments).
		for task in frappe.get_all("Task", filters={"project": doc.name}, pluck="name"):
			frappe.delete_doc("Task", task, ignore_permissions=True, force=True)

		# Work Packages.
		for wp in frappe.get_all("Work Package", filters={"project": doc.name}, pluck="name"):
			frappe.delete_doc("Work Package", wp, ignore_permissions=True, force=True)
	finally:
		frappe.flags.buildsuite_cascading = previous

	# Attachments on this project.
	_delete_attachments("Project", doc.name)


def _delete_attachments(doctype, name):
	for f in frappe.get_all(
		"File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, pluck="name"
	):
		frappe.delete_doc("File", f, ignore_permissions=True, force=True)


def backfill_project_status(doc=None, method=None):
	"""Populate project_status on Projects where it's empty (idempotent).

	Derives from native status: Completed -> Completed, else New. Wired into
	after_migrate so existing projects render a badge.
	"""
	rows = frappe.get_all("Project", fields=["name", "status", "project_status"])
	updated = 0
	for row in rows:
		if row.project_status:
			continue
		new_status = "Completed" if row.status == "Completed" else "New"
		frappe.db.set_value("Project", row.name, "project_status", new_status, update_modified=False)
		updated += 1
	if updated:
		# Batch backfill run via `bench execute`/patch, which does not auto-commit;
		# the explicit commit persists the status updates.
		frappe.db.commit()  # nosemgrep
	return updated


def seed_from_template_on_insert(doc, method=None):
	"""Seed Work Packages, Tasks and Stages onto a new project from the ERPNext
	Project Template that matches its Project Category. Each layer is opt-in via the
	Project's seed flags; tasks link to the seeded Work Package by the template task's
	work-package code. Applies to sub-projects too — seeding is driven purely by the
	seed flags, so a sub-project that ticks them gets its category's template as well.
	"""
	category = doc.get("project_category")
	if not category:
		return

	seed_wps = bool(doc.get("custom_seed_default_work_packages"))
	seed_stages = bool(doc.get("custom_seed_default_stages"))
	seed_tasks = bool(doc.get("custom_seed_default_tasks"))
	if not (seed_wps or seed_stages or seed_tasks):
		return

	template_name = frappe.db.get_value("Project Template", {"project_category": category}, "name")
	if not template_name:
		return
	template = frappe.get_doc("Project Template", template_name)
	_apply_template(doc, template, seed_wps, seed_stages, seed_tasks)


def _template_value(row, field, default=None):
	if hasattr(row, "get"):
		return row.get(field, default)
	return getattr(row, field, default)


def _template_error(reason, message, exc=None):
	"""Refuse an import with a stable reason and a user-safe message."""
	if exc is not None:
		if isinstance(exc, frappe.ValidationError) and len(exc.args) == 1 and isinstance(exc.args[0], str):
			detail = frappe.utils.strip_html_tags(str(exc)).strip()
			if detail:
				message = f"{message} {detail}"
		else:
			frappe.log_error(
				title="Project template import",
				message=frappe.get_traceback(),
			)
	frappe.throw(f"{reason}: {message}", frappe.ValidationError)


def _template_import_plan(project_doc, template, seed_wps, seed_stages, seed_tasks):
	"""Build the read-only preview used by both preview and productive imports."""
	existing_wps = {
		wp.code: wp.name
		for wp in frappe.get_all(
			"Work Package", filters={"project": project_doc.name}, fields=["name", "code"]
		)
		if wp.code
	}
	work_packages_created = []
	work_packages_reused = []
	skipped = []
	if seed_wps:
		for row in sorted(template.custom_work_packages or [], key=lambda r: r.sort_order or 0):
			if row.code in existing_wps:
				work_packages_reused.append(row.code)
				skipped.append(
					{
						"kind": "work_package",
						"code": row.code,
						"reason": "TEMPLATE_WP_REUSED",
						"message": _("Work package {0} already exists and will be reused.").format(row.code),
					}
				)
			else:
				work_packages_created.append(row.code)

	stage_by_name = {stage.stage_name: stage for stage in (template.custom_stages or [])}
	base = project_doc.expected_start_date or frappe.utils.today()

	task_rows = []
	dependencies = []
	template_task_names = {row.task for row in (template.tasks or [])}
	if seed_tasks:
		add_days = frappe.utils.add_days
		cint = frappe.utils.cint

		for row in template.tasks or []:
			try:
				template_task = frappe.get_doc("Task", row.task)
				task_start = _template_value(template_task, "start")
				task_duration = _template_value(template_task, "duration")
				stage_name = _template_value(row, "custom_stage")
				stage = stage_by_name.get(stage_name)
				if (
					task_start is None
					or task_duration is None
					or (cint(task_start) == 0 and cint(task_duration) == 0)
				):
					if stage is not None:
						start_offset = cint(_template_value(stage, "offset_start_days"))
						end_offset = cint(_template_value(stage, "offset_end_days"))
						exp_start_date = add_days(base, start_offset)
						exp_end_date = add_days(base, end_offset)
					else:
						exp_start_date = None
						exp_end_date = None
				else:
					start_offset = cint(task_start)
					duration = cint(task_duration)
					exp_start_date = add_days(base, start_offset)
					exp_end_date = add_days(exp_start_date, duration)

				task_rows.append(
					{
						"template_task": row.task,
						"subject": template_task.subject,
						"priority": template_task.priority or "Medium",
						"expected_time": template_task.expected_time or 0,
						"work_package_code": _template_value(row, "custom_work_package_code"),
						"stage": stage_name,
						"exp_start_date": exp_start_date,
						"exp_end_date": exp_end_date,
					}
				)
				for dependency in template_task.get("depends_on") or []:
					if dependency.task not in template_task_names:
						_template_error(
							"TEMPLATE_DEPENDENCY_NOT_IN_TEMPLATE",
							_("Task {0} depends on a task that is not in this template.").format(
								template_task.subject
							),
						)
					dependencies.append(
						{
							"task": row.task,
							"depends_on": dependency.task,
							"dependency_type": dependency.get("dependency_type") or "FS",
							"lag_days": cint(dependency.get("lag_days")),
						}
					)
			except Exception as exc:
				_template_error(
					"TEMPLATE_TASK_PLAN_FAILED",
					_("Could not plan task {0}.").format(row.task),
					exc,
				)

	end_dates = [task_row["exp_end_date"] for task_row in task_rows if task_row["exp_end_date"]]
	computed_finish = max(end_dates) if end_dates else None
	return {
		"existing_wps": existing_wps,
		"work_packages_created": work_packages_created,
		"work_packages_reused": work_packages_reused,
		"task_rows": task_rows,
		"dependencies": dependencies,
		"computed_finish": computed_finish,
		"skipped": skipped,
		"stage_count": len(template.custom_stages or []) if seed_stages else 0,
	}


def _apply_template(project_doc, template, seed_wps, seed_stages, seed_tasks, plan=None):
	"""Append a Project Template's Work Packages, dated Tasks and Stages.

	This is shared by create-time seeding and the explicit import endpoint. It is
	strict: the caller's transaction owns rollback, so no row failure is swallowed.
	"""
	plan = plan or _template_import_plan(project_doc, template, seed_wps, seed_stages, seed_tasks)
	created = {
		"work_packages": 0,
		"tasks": 0,
		"stages": 0,
		"work_packages_created": list(plan["work_packages_created"]),
		"work_packages_reused": list(plan["work_packages_reused"]),
		"task_rows": list(plan["task_rows"]),
		"dependencies": list(plan["dependencies"]),
		"computed_finish": plan["computed_finish"],
		"skipped": list(plan["skipped"]),
	}

	wp_by_code = dict(plan["existing_wps"])
	wp_rows = {row.code: row for row in (template.custom_work_packages or [])}
	for code in plan["work_packages_created"]:
		row = wp_rows[code]
		try:
			wp = frappe.get_doc(
				{
					"doctype": "Work Package",
					"project": project_doc.name,
					"code": row.code,
					"work_package_name": row.work_package_name,
					"budget": row.budget or 0,
					"description": row.description,
					"status": "Planned",
				}
			).insert(ignore_permissions=True)
		except frappe.ValidationError:
			raise
		except Exception as exc:
			_template_error(
				"TEMPLATE_WORK_PACKAGE_CREATE_FAILED",
				_("Could not import work package {0}.").format(code),
				exc,
			)
		wp_by_code[code] = wp.name
		created["work_packages"] += 1

	tasks_by_stage = {}
	task_by_template = {}
	if seed_tasks:
		for task_row in plan["task_rows"]:
			try:
				task = frappe.get_doc(
					{
						"doctype": "Task",
						"project": project_doc.name,
						"subject": task_row["subject"],
						"priority": task_row["priority"],
						"expected_time": task_row["expected_time"],
						"work_package": wp_by_code.get(task_row["work_package_code"]),
						"task_status": "Yet To Start",
						"exp_start_date": task_row["exp_start_date"],
						"exp_end_date": task_row["exp_end_date"],
						"template_task": task_row["template_task"],
					}
				).insert(ignore_permissions=True)
			except Exception as exc:
				_template_error(
					"TEMPLATE_TASK_CREATE_FAILED",
					_("Could not import task '{0}'.").format(task_row["subject"]),
					exc,
				)
			task_by_template[task_row["template_task"]] = task
			created["tasks"] += 1
			if task_row["stage"]:
				tasks_by_stage.setdefault(task_row["stage"], []).append(task.name)

		for dependency in plan["dependencies"]:
			try:
				task = task_by_template[dependency["task"]]
				task.append(
					"depends_on",
					{
						"task": task_by_template[dependency["depends_on"]].name,
						"dependency_type": dependency["dependency_type"],
						"lag_days": dependency["lag_days"],
					},
				)
				task.save(ignore_permissions=True)
			except Exception as exc:
				_template_error(
					"TEMPLATE_DEPENDENCY_CREATE_FAILED",
					_("Could not copy dependencies for task '{0}'.").format(dependency["task"]),
					exc,
				)

	if seed_stages:
		try:
			created["stages"] = _seed_stages_from_template(project_doc, template, tasks_by_stage)
		except Exception as exc:
			_template_error(
				"TEMPLATE_STAGE_CREATE_FAILED",
				_("Could not import the project stages."),
				exc,
			)

	return created


@frappe.whitelist()
def import_project_template(
	project: str,
	work_packages=0,
	stages=0,
	tasks=0,
	project_category: str | None = None,
	test_run: int | bool = 1,
):
	"""Preview or atomically import a Project Template into an existing project."""
	cint = frappe.utils.cint

	doc = frappe.get_doc("Project", project)
	doc.check_permission("write")

	category = project_category or doc.get("project_category")
	if not category:
		frappe.throw(_("This project has no Project Category, so there is no template to import."))
	template_name = frappe.db.get_value("Project Template", {"project_category": category}, "name")
	if not template_name:
		frappe.throw(_("No template is configured for category {0}.").format(category))

	seed_wps, seed_stages, seed_tasks = cint(work_packages), cint(stages), cint(tasks)
	if not (seed_wps or seed_stages or seed_tasks):
		frappe.throw(_("Choose at least one of Work Packages, Stages or Tasks to import."))

	template = frappe.get_doc("Project Template", template_name)
	plan = _template_import_plan(doc, template, bool(seed_wps), bool(seed_stages), bool(seed_tasks))
	if cint(test_run):
		return {
			"ok": True,
			"category": category,
			"work_packages": len(plan["work_packages_created"]),
			"tasks": len(plan["task_rows"]),
			"stages": plan["stage_count"],
			**{
				key: plan[key]
				for key in (
					"work_packages_created",
					"work_packages_reused",
					"task_rows",
					"dependencies",
					"computed_finish",
					"skipped",
				)
			},
		}

	message_log = getattr(frappe.local, "message_log", None)
	prior_log_len = len(message_log) if isinstance(message_log, list) else None
	prior_cascade = frappe.flags.get("in_schedule_cascade")
	frappe.db.savepoint("template_import")
	frappe.flags.in_schedule_cascade = True
	try:
		created = _apply_template(
			doc, template, bool(seed_wps), bool(seed_stages), bool(seed_tasks), plan=plan
		)
	except frappe.ValidationError:
		frappe.db.rollback(save_point="template_import")
		if prior_log_len is not None:
			del message_log[prior_log_len:]
		raise
	except Exception as exc:
		frappe.db.rollback(save_point="template_import")
		if prior_log_len is not None:
			del message_log[prior_log_len:]
		_template_error(
			"TEMPLATE_IMPORT_FAILED",
			_("Could not import this project template. Please try again or contact your administrator."),
			exc,
		)
	finally:
		frappe.flags.in_schedule_cascade = prior_cascade

	return {"ok": True, "category": category, **created}


@frappe.whitelist()
def get_project_template_summary(project_category: str | None = None, project_type: str | None = None):
	"""Preview of the template for a Project Category (used by the create form):
	its stage names, work-package count and task count. `project_type` is accepted
	for backward compatibility with older callers."""
	category = project_category or project_type
	if not category:
		return {"exists": False}

	template_name = frappe.db.get_value("Project Template", {"project_category": category}, "name")
	if not template_name:
		return {"exists": False}

	template = frappe.get_doc("Project Template", template_name)
	stage_names = [s.stage_name for s in (template.custom_stages or [])]
	return {
		"exists": True,
		"stage_names": stage_names,
		"stage_count": len(stage_names),
		"work_package_count": len(template.custom_work_packages or []),
		"task_count": len(template.tasks or []),
	}


def _seed_stages_from_template(project_doc, template, tasks_by_stage=None):
	"""Append the template's stages onto a project, dates clamped to the project's
	own window so date-bounds validation passes. When tasks_by_stage maps a stage
	name to seeded Task names, those tasks are added to the stage's task list (at a
	default planned qty of 100%). Returns the count of stages created."""
	from frappe.utils import add_days, getdate, today

	tasks_by_stage = tasks_by_stage or {}
	base = project_doc.expected_start_date or today()
	end_bound = project_doc.expected_end_date

	def _clamp(d):
		if end_bound and getdate(d) > getdate(end_bound):
			return end_bound
		return d

	count = 0
	for row in template.custom_stages:
		stage = frappe.get_doc(
			{
				"doctype": "Stage Planning",
				"project": project_doc.name,
				"stage_name": row.stage_name,
				"planned_start": _clamp(add_days(base, row.offset_start_days or 0)),
				"planned_end": _clamp(add_days(base, row.offset_end_days or 0)),
				"planned_task_count": row.planned_task_count or 0,
				"planned_completion_pct": row.planned_completion_pct or 0,
				"workflow_state": "Draft",
			}
		)
		for task_name in tasks_by_stage.get(row.stage_name, []):
			stage.append("stage_planning_tasks", {"task": task_name, "planned_qty": 100, "qty_unit": "%"})
		stage.insert(ignore_permissions=True)
		count += 1
	return count


@frappe.whitelist()
def seed_stages_from_template(project: str):
	"""Append the matching category template's stages onto an EXISTING project.
	Mirrors the create-time seed. Returns the number of stages created."""
	doc = frappe.get_doc("Project", project)
	doc.check_permission("write")

	category = doc.get("project_category")
	if not category:
		frappe.throw(_("This project has no Project Category, so there is no template to seed from."))

	template_name = frappe.db.get_value("Project Template", {"project_category": category}, "name")
	if not template_name:
		frappe.throw(_("No template is configured for category {0}.").format(category))

	template = frappe.get_doc("Project Template", template_name)

	# Attach the project's existing tasks to the stage they belong to in the
	# template (matched by subject), so re-seeded stages arrive with their tasks
	# just like the create-time import does.
	stage_by_subject = {}
	for row in template.tasks:
		subject = frappe.db.get_value("Task", row.task, "subject")
		if subject and row.get("custom_stage"):
			stage_by_subject[subject] = row.custom_stage
	tasks_by_stage = {}
	for t in frappe.get_all("Task", filters={"project": doc.name}, fields=["name", "subject"]):
		stage = stage_by_subject.get(t.subject)
		if stage:
			tasks_by_stage.setdefault(stage, []).append(t.name)

	return {"seeded": _seed_stages_from_template(doc, template, tasks_by_stage)}


def ensure_project_team_membership(doc, method=None):
	"""PRM-002 — project visibility is team-membership based. Ensure the project's
	creator and its assigned project_manager are team members, so the project is
	visible to them under the team-scoped permission model. Idempotent; inserts the
	child rows directly to avoid re-triggering the parent's on_update.
	"""
	_ensure_team_member(doc.name, doc.owner)
	_ensure_team_member(doc.name, getattr(doc, "project_manager", None))


def _ensure_team_member(project_name, user):
	if not user or user == "Administrator":
		return
	if not frappe.db.exists("User", user):
		return
	if frappe.db.exists(
		"Project Team",
		{
			"parenttype": "Project",
			"parentfield": "custom_team_members",
			"parent": project_name,
			"user": user,
		},
	):
		return
	frappe.get_doc(
		{
			"doctype": "Project Team",
			"parenttype": "Project",
			"parentfield": "custom_team_members",
			"parent": project_name,
			"user": user,
			"full_name": frappe.db.get_value("User", user, "full_name"),
		}
	).insert(ignore_permissions=True)


@frappe.whitelist()
def create_warehouse_for_project(doc: Document, method: str | None = None):
	# Create Projects Group Warehouse
	if not frappe.db.exists("Warehouse", {"warehouse_name": "Projects", "company": doc.company}):
		warehouse = frappe.new_doc("Warehouse")
		warehouse.warehouse_name = "Projects"
		warehouse.company = doc.company
		warehouse.is_group = 1
		warehouse.insert(ignore_permissions=True)

	parent_warehouse = frappe.db.get_value(
		"Warehouse", {"warehouse_name": "Projects", "company": doc.company}, "name"
	)

	# Create Project Group Warehouse
	if not frappe.db.exists("Warehouse", {"warehouse_name": doc.project_name, "company": doc.company}):
		warehouse = frappe.new_doc("Warehouse")
		warehouse.warehouse_name = doc.project_name
		warehouse.parent_warehouse = parent_warehouse
		warehouse.company = doc.company
		warehouse.is_group = 1
		warehouse.insert(ignore_permissions=True)

	project_parent_warehouse = frappe.db.get_value(
		"Warehouse", {"warehouse_name": doc.project_name, "company": doc.company}, "name"
	)

	# Create Store Warehouse
	project_warehouse = f"{doc.project_name} Store"

	if not frappe.db.exists("Warehouse", {"warehouse_name": project_warehouse, "company": doc.company}):
		stock_account = frappe.db.get_value(
			"Account", {"account_type": "Stock", "company": doc.company, "is_group": 0}, "name"
		)

		warehouse = frappe.new_doc("Warehouse")
		warehouse.warehouse_name = project_warehouse
		warehouse.project = doc.name
		warehouse.parent_warehouse = project_parent_warehouse
		warehouse.account = stock_account
		warehouse.company = doc.company
		warehouse.insert(ignore_permissions=True)


@frappe.whitelist()
def delete_warehouse_for_project(doc: Document, method: str | None = None):
	"""Remove the per-project warehouses created in create_warehouse_for_project so a
	project delete leaves no orphans: the "<project> Store" leaf first, then its
	"<project>" group parent once empty. The shared company-level "Projects" group is
	left intact — other projects hang off it."""
	store = frappe.db.get_value(
		"Warehouse", {"warehouse_name": f"{doc.project_name} Store", "company": doc.company}, "name"
	)
	if store:
		frappe.delete_doc("Warehouse", store, ignore_permissions=True, force=True)

	# The per-project group warehouse (parent of the Store) was previously left
	# orphaned. Remove it too, but only once it has no remaining child warehouses.
	group = frappe.db.get_value(
		"Warehouse",
		{"warehouse_name": doc.project_name, "company": doc.company, "is_group": 1},
		"name",
	)
	if group and not frappe.db.count("Warehouse", {"parent_warehouse": group}):
		frappe.delete_doc("Warehouse", group, ignore_permissions=True, force=True)
