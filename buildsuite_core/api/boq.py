# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

"""BOQ operations that aren't a single-document save — workflow transitions,
Assembly explosion, revisions, clone, template import and actuals recompute.
Reads go through the standard data adapter; only these mutations need the API.
"""

import hashlib
import hmac
import html
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, flt, today

from buildsuite_core.buildsuite_core.doctype.boq.boq_rollup import recompute_boq
from buildsuite_core.permissions.setup import BOQ_APPROVE_ROLES


def _require_write(boq):
	if not frappe.has_permission("BOQ", "write", doc=boq):
		frappe.throw("BOQ_WRITE_NOT_PERMITTED", frappe.PermissionError)


def _require_draft(doc):
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft BOQ can be edited — this one is {0}.").format(doc.status))


# --- workflow -------------------------------------------------------------


@frappe.whitelist()
def submit_boq(boq: str):
	"""Draft -> Submitted."""
	_require_write(boq)
	doc = frappe.get_doc("BOQ", boq)
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft BOQ can be submitted for approval."))
	doc.status = "Submitted"
	doc.save(ignore_permissions=True)
	return doc.status


def _boq_codes(boq):
	"""(group_codes, item_codes) present in a BOQ revision — the join keys."""
	groups = [c for c in frappe.get_all("BOQ Group", filters={"boq": boq}, pluck="code") if c]
	items = [c for c in frappe.get_all("BOQ Item", filters={"boq": boq}, pluck="code") if c]
	return groups, items


def _validate_codes_for_approval(doc):
	"""R9 — the requirement that makes the rest true. Cost code is the only join key, so every
	other rule assumes codes are stable. Block approval if two lines share a code, or if a code
	that carries actuals in the current Approved revision is absent from this one (quietly
	renumbered/dropped rather than retired with its code kept)."""
	groups, items = _boq_codes(doc.name)
	dupes = sorted({c for c in groups if groups.count(c) > 1} | {c for c in items if items.count(c) > 1})
	if dupes:
		frappe.throw(
			_("Duplicate cost codes in this BOQ: {0}. Every line needs a unique code.").format(
				", ".join(dupes)
			)
		)

	# A project-less estimate stands alone — there is no sibling "current Approved revision of the
	# same project" to protect cost codes against, and filtering by a blank project would wrongly
	# match every other estimate. The duplicate-code guard above still applies.
	if not doc.project:
		return

	prior = frappe.db.get_value(
		"BOQ", {"project": doc.project, "status": "Approved", "name": ("!=", doc.name)}, "name"
	)
	if not prior:
		return
	from buildsuite_core.api.boq_actuals import _actual_entries

	prior_codes = set().union(*[set(x) for x in _boq_codes(prior)])
	with_actuals = {
		c
		for e in _actual_entries(doc.project)
		for c in (e["group_code"], e["item_code"])
		if c and c in prior_codes
	}
	missing = sorted(with_actuals - (set(groups) | set(items)))
	if missing:
		frappe.throw(
			_(
				"These cost codes carry actuals in the current Approved revision but are absent "
				"from this one: {0}. Keep the code (retire the line) or restore it — codes are "
				"immutable once approved."
			).format(", ".join(missing))
		)


@frappe.whitelist()
def approve_boq(boq: str):
	"""Approve a Submitted BOQ; supersede any other Approved revision of the same
	project. Gated on BOQ_APPROVE_ROLES (server-authoritative)."""
	if not set(frappe.get_roles()) & set(BOQ_APPROVE_ROLES):
		frappe.throw(_("You are not permitted to approve a BOQ."), frappe.PermissionError)
	doc = frappe.get_doc("BOQ", boq)
	if doc.status not in ("Submitted", "Draft"):
		frappe.throw(_("Only a Draft or Submitted BOQ can be approved."))
	_validate_codes_for_approval(doc)
	# Supersede the other Approved revision of the SAME project. A project-less estimate has no
	# such sibling (a blank project would match every other estimate), so it supersedes nothing.
	if doc.project:
		for other in frappe.get_all(
			"BOQ",
			filters={"project": doc.project, "status": "Approved", "name": ("!=", doc.name)},
			pluck="name",
		):
			frappe.db.set_value("BOQ", other, "status", "Superseded")
	doc.status = "Approved"
	doc.approved_by = frappe.session.user
	doc.approved_date = today()
	doc.save(ignore_permissions=True)
	return doc.status


# --- attach an estimate to a project --------------------------------------


def _attach_blocker(src, project: str) -> str | None:
	"""Why this estimate can't attach to ``project``, or None if it can. A project carries one
	baseline, so a project that already has an Approved BOQ is not a target."""
	if src.project:
		return _("This estimate is already attached to a project.")
	if not project or not frappe.db.exists("Project", project):
		return _("Pick a project to attach this estimate to.")
	existing = frappe.db.get_value("BOQ", {"project": project, "status": "Approved"}, "name")
	if existing:
		return _("{0} already has an approved BOQ ({1}) — a project carries one baseline.").format(
			project, existing
		)
	return None


@frappe.whitelist()
def attach_estimate_to_project(boq: str, project: str):
	"""Attach a project-less estimate (and its whole revision lineage) to a project once the work
	is won. Re-anchors each revision's company to the project (the controller derives it) and sets
	the customer from the project. Returns ``{moved, project}``."""
	src = frappe.get_doc("BOQ", boq)
	if not frappe.has_permission("BOQ", "write", doc=src):
		frappe.throw(_("You are not permitted to modify this BOQ."), frappe.PermissionError)
	blocker = _attach_blocker(src, project)
	if blocker:
		frappe.throw(blocker)
	customer = frappe.db.get_value("Project", project, "customer")
	# Move the lineage parent-first (revision asc) so the same-company guard never trips on a
	# child whose base_revision hasn't moved yet. The controller re-anchors company on save.
	peers = frappe.get_all(
		"BOQ", filters={"name": ("in", _revision_peers(src))}, pluck="name", order_by="revision asc"
	)
	for name in peers:
		peer = frappe.get_doc("BOQ", name)
		peer.project = project
		if customer:
			peer.customer = customer
		peer.save(ignore_permissions=True)
	return {"moved": len(peers), "project": project}


# --- bid from an approved estimate ----------------------------------------

BID_KINDS = ("Quotation", "Tender")
# Every ERPNext sales line hangs off an Item; a BOQ line is a scope of work, not stock, so
# the Quotation points every line at one generic service item and the description carries the
# real text (mirrors the SPA's New Quotation form).
_QUOTATION_SERVICE_ITEM = "Professional Services"


def _lump_sum_uom() -> str:
	"""The UOM for a group-mode lump-sum line. Both the Tender and the Quotation link `unit`/`uom`
	to the UOM master, so a plain "LS" string fails validation — ensure a real "Lump Sum" UOM
	(idempotent; mirrors the import flow that creates missing units on demand)."""
	name = "Lump Sum"
	if not frappe.db.exists("UOM", name):
		frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(ignore_permissions=True)
	return name


def _bid_lines_from_boq(boq_name: str, line_mode: str) -> list[dict]:
	"""Snapshot the BOQ into bid lines. ``item`` = one line per BOQ item; ``group`` = one lump-sum
	line per group (rate = sum of the group's planned amounts). Group lines with no value drop out.
	"""
	line_mode = "group" if line_mode == "group" else "item"
	groups = frappe.get_all(
		"BOQ Group",
		filters={"boq": boq_name},
		fields=["name", "code", "group_name"],
		order_by="idx asc, creation asc",
	)
	lines = []
	if line_mode == "group":
		lump_sum = _lump_sum_uom()
		for g in groups:
			items = frappe.get_all("BOQ Item", filters={"boq_group": g.name}, pluck="planned_amount")
			amount = sum(flt(a) for a in items)
			if amount > 0:
				lines.append(
					{
						"source_ref": g.name,
						"code": g.code,
						"description": g.group_name,
						"unit": lump_sum,
						"qty": 1,
						"rate": flt(amount, 2),
					}
				)
	else:
		for g in groups:
			for i in frappe.get_all(
				"BOQ Item",
				filters={"boq_group": g.name},
				fields=["name", "code", "description", "unit", "planned_qty", "rate"],
				order_by="idx asc, creation asc",
			):
				lines.append(
					{
						"source_ref": i.name,
						"code": i.code,
						"description": i.description,
						"unit": i.unit or "Nos",
						"qty": flt(i.planned_qty),
						"rate": flt(i.rate),
					}
				)
	return lines


@frappe.whitelist()
def create_bid_from_boq(
	boq: str,
	kind: str,
	line_mode: str = "item",
	title: str | None = None,
	submission_deadline: str | None = None,
):
	"""Raise a bid (a Quotation or a Tender) from an Approved BOQ — the terminal step of the
	estimate workflow: price it, approve it, then bid it when you pursue the work. Returns
	``{doctype, name}`` so the SPA routes to the new document."""
	kind = (kind or "").strip()
	if kind not in BID_KINDS:
		frappe.throw(_("A bid is a Quotation or a Tender."))
	src = frappe.get_doc("BOQ", boq)
	if src.status != "Approved":
		frappe.throw(_("Only an Approved BOQ can become a bid."))
	target = "BuildSuite Tenders" if kind == "Tender" else "Quotation"
	if not frappe.has_permission(target, "create"):
		frappe.throw(_("You are not permitted to create a {0}.").format(kind), frappe.PermissionError)
	lines = _bid_lines_from_boq(src.name, line_mode)
	if not lines:
		frappe.throw(_("This BOQ has no priced lines to bid."))
	title = (title or src.title or "").strip()
	name = (
		_tender_from_boq(src, lines, title, submission_deadline)
		if kind == "Tender"
		else _quotation_from_boq(src, lines, title)
	)
	return {"doctype": target, "name": name}


def _tender_from_boq(src, lines: list[dict], title: str, submission_deadline: str | None) -> str:
	"""A BuildSuite Tender whose item rows carry the BOQ lines. The Tender controller derives
	sell rate / amount / totals from the cost ``rate`` + ``margin_percent`` on validate.

	A Tender hard-requires a submission deadline, an issuing body and a reference; the deadline
	comes from the bid form, and the issuing body / reference are seeded from the estimate (the
	customer and the BOQ id) for the user to refine on the Tender form."""
	if not submission_deadline:
		frappe.throw(_("A tender needs a submission deadline."))
	doc = frappe.get_doc(
		{
			"doctype": "BuildSuite Tenders",
			"title": title,
			"issuing_body": src.customer or _("To be confirmed"),
			"tender_reference": src.name,
			"project": src.project or None,
			"company": src.company,
			"date_issued": today(),
			"submission_deadline": submission_deadline,
			"margin_percent": flt(src.margin_rate),
			"tax_percent": flt(src.tax_rate),
			"source_boq": src.name,
			"source_boq_revision": src.revision,
			"buildsuite_tenders_items": [
				{
					"source": "BOQ",
					"source_ref": line["source_ref"],
					"code": line["code"],
					"description": line["description"],
					"unit": line["unit"],
					"qty": line["qty"],
					"rate": line["rate"],
				}
				for line in lines
			],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _quotation_from_boq(src, lines: list[dict], title: str) -> str:
	"""An ERPNext Quotation for the estimate's customer, every line on the generic service item
	with the BOQ text + cost as price-list rate and the BOQ margin applied (ERPNext derives the
	sell rate). A Quotation is party-bound, so the estimate needs a customer."""
	if not src.customer:
		frappe.throw(_("Set a customer on the estimate to raise a quotation (or raise a tender instead)."))
	margin = flt(src.margin_rate)
	doc = frappe.get_doc(
		{
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": src.customer,
			"title": title,
			"project": src.project or None,
			"source_boq": src.name,
			"source_boq_revision": src.revision,
			"transaction_date": today(),
			"items": [
				{
					"item_code": _QUOTATION_SERVICE_ITEM,
					"item_name": (line["description"] or line["code"] or "Item")[:140],
					"description": line["description"],
					# A synthetic lump-sum unit (group mode) isn't a UOM master; fall back to Nos.
					"uom": line["unit"] if frappe.db.exists("UOM", line["unit"]) else "Nos",
					"qty": line["qty"] or 1,
					"price_list_rate": line["rate"],
					"margin_type": "Percentage",
					"margin_rate_or_amount": margin,
					"source": "BOQ",
					"code": line["code"],
					"source_ref": line["source_ref"],
				}
				for line in lines
			],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


# --- project from an approved estimate ------------------------------------


def _task_subject(description: str | None, code: str | None) -> str:
	"""A Task subject from a BOQ item: its first line / sentence, capped to the Task subject
	length, falling back to the item code."""
	text = (description or "").strip()
	if not text:
		return (code or _("Task"))[:140]
	first = text.splitlines()[0]
	for sep in (". ", "; "):
		if sep in first:
			first = first.split(sep, 1)[0]
			break
	return (first or code or _("Task"))[:140]


def _set_up_work_from_boq(src, work: str, group_ids=None) -> dict:
	"""Create Work Packages (one per BOQ group) and, unless ``work == 'packages'``, a Task per BOQ
	item, on the project this estimate is attached to. Maps BOQ group -> Work Package and BOQ item
	-> Task; sub-items only inherit the group's Work Package. Idempotent: a Work Package is matched
	by ``code`` (reused, never duplicated) and an item that already carries a live Task is skipped.
	Each item is stamped with its Work Package (and its sub-items) and, for tasks, its Task. Returns
	the count of each NEWLY created. The caller is responsible for the BOQ rollup afterwards."""
	project = src.get("project")
	if not project:
		frappe.throw(_("Attach this estimate to a project first."))
	want_tasks = work != "packages"
	selected = set(group_ids or [])

	# Existing Work Packages by code, so a re-run reuses them (mirror _apply_template).
	wp_by_code = {
		wp.code: wp.name
		for wp in frappe.get_all("Work Package", filters={"project": project}, fields=["name", "code"])
		if wp.code
	}
	out = {"work_packages": 0, "tasks": 0}
	groups = frappe.get_all(
		"BOQ Group",
		filters={"boq": src.name},
		fields=["name", "code", "group_name"],
		order_by="idx_order asc, creation asc",
	)
	for g in groups:
		if selected and g.name not in selected:
			continue
		items = frappe.get_all(
			"BOQ Item",
			filters={"boq_group": g.name},
			fields=["name", "code", "description", "planned_amount", "work_package", "task"],
			order_by="idx asc, creation asc",
		)
		# Work Package for this group — reuse one with the same code, else create it.
		wp_name = wp_by_code.get(g.code)
		if not wp_name:
			budget = sum(flt(i.planned_amount) for i in items)
			wp = frappe.get_doc(
				{
					"doctype": "Work Package",
					"project": project,
					"code": g.code,
					"work_package_name": g.group_name or g.code,
					"budget": budget,
					"description": _("From estimate {0}, group {1}.").format(src.name, g.code),
					"status": "Planned",
				}
			).insert(ignore_permissions=True)
			wp_name = wp.name
			wp_by_code[g.code] = wp_name
			out["work_packages"] += 1

		for it in items:
			# Stamp the Work Package onto the item (and its sub-items) unless already tagged.
			if not it.work_package:
				frappe.db.set_value("BOQ Item", it.name, "work_package", wp_name, update_modified=False)
				for si in frappe.get_all("BOQ Sub Item", filters={"boq_item": it.name}, pluck="name"):
					frappe.db.set_value("BOQ Sub Item", si, "work_package", wp_name, update_modified=False)
			if not want_tasks:
				continue
			if it.task and frappe.db.exists("Task", it.task):
				continue  # already has a live Task
			task = frappe.get_doc(
				{
					"doctype": "Task",
					"project": project,
					"subject": _task_subject(it.description, it.code),
					"work_package": it.work_package or wp_name,
					"priority": "Medium",
					"task_status": "Yet To Start",
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value("BOQ Item", it.name, "task", task.name, update_modified=False)
			out["tasks"] += 1
	return out


def _won_offer_for_boq(src):
	"""The bid a project should be made from: a Quotation or BuildSuite Tender raised from this
	estimate's lineage (``source_boq``) that isn't attached to a project yet. Live has no formal
	"awarded" status, so a raised, still-unattached bid is taken as the winning one; the most recent
	wins. Returns ``{kind, doctype, name, taxable}`` (``taxable`` = the priced total before tax) or
	``None``."""
	lineage = _revision_peers(src)
	if not lineage:
		return None
	cands = []
	# Quotation prices into net_total (pre-tax); a BuildSuite Tender into bid_before_tax.
	for doctype, kind, total_field, date_field in (
		("BuildSuite Tenders", "Tender", "bid_before_tax", "date_issued"),
		("Quotation", "Quotation", "net_total", "transaction_date"),
	):
		filters = {"source_boq": ("in", lineage), "project": ("in", ["", None])}
		fields = ["name", f"{total_field} as taxable", f"{date_field} as decided"]
		if doctype == "Quotation":
			# A quotation the client turned down, or one that was cancelled, never wins.
			filters["docstatus"] = ("!=", 2)
			filters["status"] = ("!=", "Lost")
			fields.append("accepted_on")
		for r in frappe.get_all(doctype, filters=filters, fields=fields):
			cands.append(
				{
					"kind": kind,
					"doctype": doctype,
					"name": r.name,
					"taxable": flt(r.taxable),
					"decided": str(r.decided or ""),
					"accepted": bool(r.get("accepted_on")),
				}
			)
	if not cands:
		return None
	# An accepted quotation is the winning one; otherwise the most recent raised bid.
	cands.sort(key=lambda c: (c["accepted"], c["decided"]), reverse=True)
	top = cands[0]
	return {"kind": top["kind"], "doctype": top["doctype"], "name": top["name"], "taxable": top["taxable"]}


def _customer_of_won_quotation(won):
	"""The customer a won quotation is with. A quotation to a lead names the lead; the project and
	its invoices need a customer, so a lead that has not become one yet stops the project."""
	if not won or won["doctype"] != "Quotation":
		return None
	q = frappe.db.get_value("Quotation", won["name"], ["quotation_to", "party_name"], as_dict=True)
	if q.quotation_to == "Customer":
		return q.party_name
	if q.quotation_to == "Lead":
		customer = frappe.db.get_value("Customer", {"lead_name": q.party_name})
		if not customer:
			frappe.throw(
				_(
					"Quotation {0} was raised for lead {1}, who is not a customer yet. "
					"Accept the quotation, or convert the lead, before creating the project."
				).format(won["name"], q.party_name)
			)
		return customer
	return None


@frappe.whitelist()
def create_project_from_boq(
	boq: str,
	name: str | None = None,
	code: str | None = None,
	project_category: str | None = None,
	project_type: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
	pm: str | None = None,
	work: str = "tasks",
	group_ids=None,
):
	"""Create a Project from an Approved, project-less BOQ, mapping its groups to Work Packages and
	(unless ``work`` is ``'packages'`` / ``'none'``) its items to Tasks. Relinks the whole revision
	lineage to the new project. Returns ``{project, project_name, work}``."""
	src = frappe.get_doc("BOQ", boq)
	_require_write(src)
	if src.status != "Approved":
		frappe.throw(_("Only an Approved BOQ can become a project."))
	if src.get("project"):
		frappe.throw(_("This estimate is already attached to a project."))
	if not frappe.has_permission("Project", "create"):
		frappe.throw(_("You are not permitted to create a project."), frappe.PermissionError)
	if work != "none" and not frappe.has_permission("Work Package", "create"):
		frappe.throw(_("You are not permitted to create work packages."), frappe.PermissionError)

	group_ids = frappe.parse_json(group_ids) if isinstance(group_ids, str) else group_ids
	project_name = (name or src.title or "").strip()
	if not project_name:
		frappe.throw(_("Give the project a name."))

	# A bid raised from this estimate (not yet attached to a project) carries the agreed revenue and
	# the payment schedule onto the project; otherwise the contract value is the estimate's own
	# priced total (planned cost + margin).
	won = _won_offer_for_boq(src)
	won_customer = _customer_of_won_quotation(won)
	contract_value = won["taxable"] if won else flt(src.planned_amount) * (1 + flt(src.margin_rate) / 100.0)

	proj = frappe.get_doc(
		{
			"doctype": "Project",
			"project_name": project_name,
			"custom_project_id": (code or "").strip() or None,
			"company": src.company,
			"customer": src.customer or won_customer or None,
			"project_category": project_category or None,
			"project_type": project_type or None,
			"expected_start_date": start_date or None,
			"expected_end_date": end_date or None,
			"estimated_costing": flt(src.planned_amount),
			"contract_value": flt(contract_value),
			"project_manager": pm or None,
			"notes": _("Created from estimate {0}.").format(src.name),
			# Build the work breakdown from this BOQ, not the category template.
			"custom_seed_default_stages": 0,
			"custom_seed_default_tasks": 0,
			"custom_seed_default_work_packages": 0,
		}
	)
	if won:
		offer = frappe.get_doc(won["doctype"], won["name"])
		if not proj.project_category:
			proj.project_category = offer.get("project_category")
		proj.payment_term_template = offer.get("payment_term_template")
		# Project / Quotation / Tender all hold the schedule as BuildSuite Payment Milestone rows,
		# so the agreed schedule copies across verbatim.
		for m in offer.get("payment_milestones") or []:
			proj.append(
				"payment_milestones",
				{
					"payment_milestone": m.payment_milestone,
					"payable": m.payable,
					"percentage": m.percentage,
					"client_sign_off": m.client_sign_off,
					"stage": m.stage,
					"days_after_project_start": m.days_after_project_start,
				},
			)
	proj.insert(ignore_permissions=True)

	# Link the won bid to the project it produced.
	if won:
		frappe.db.set_value(won["doctype"], won["name"], "project", proj.name, update_modified=False)

	# Relink the whole revision lineage to the project, parent-first (revision asc) so the
	# same-company guard never trips — mirrors attach_estimate_to_project.
	peers = frappe.get_all(
		"BOQ", filters={"name": ("in", _revision_peers(src))}, pluck="name", order_by="revision asc"
	)
	for pname in peers:
		peer = frappe.get_doc("BOQ", pname)
		peer.project = proj.name
		peer.save(ignore_permissions=True)

	result = {
		"project": proj.name,
		"project_name": proj.project_name,
		"contract_value": flt(contract_value),
		"from_offer": won,
		"work": {"work_packages": 0, "tasks": 0},
	}
	if work != "none":
		src.reload()
		result["work"] = _set_up_work_from_boq(src, work, group_ids)
		recompute_boq(src.name)
	return result


@frappe.whitelist()
def set_up_work_from_boq(boq: str, work: str = "tasks", group_ids=None):
	"""Create Work Packages and Tasks from an Approved BOQ already attached to a project — the
	"set up work packages and tasks" path. Idempotent (see ``_set_up_work_from_boq``)."""
	src = frappe.get_doc("BOQ", boq)
	_require_write(src)
	if src.status != "Approved":
		frappe.throw(_("Only an Approved BOQ can set up work."))
	if not src.get("project"):
		frappe.throw(_("Attach this estimate to a project first."))
	if not frappe.has_permission("Work Package", "create"):
		frappe.throw(_("You are not permitted to create work packages."), frappe.PermissionError)
	group_ids = frappe.parse_json(group_ids) if isinstance(group_ids, str) else group_ids
	out = _set_up_work_from_boq(src, work, group_ids)
	recompute_boq(src.name)
	return out


@frappe.whitelist()
def boq_work_plan(boq: str):
	"""The groups of a BOQ with what the "create project" modal needs: each group's amount, item
	count, whether a Work Package already exists for it on the project, and how many items still
	need a Task. Drives the per-group checklist."""
	src = frappe.get_doc("BOQ", boq)
	if not frappe.has_permission("BOQ", "read", doc=src):
		frappe.throw(_("You are not permitted to read this BOQ."), frappe.PermissionError)
	project = src.get("project")
	wp_codes = (
		set(frappe.get_all("Work Package", filters={"project": project}, pluck="code")) if project else set()
	)
	groups, total = [], 0.0
	for g in frappe.get_all(
		"BOQ Group",
		filters={"boq": src.name},
		fields=["name", "code", "group_name"],
		order_by="idx_order asc, creation asc",
	):
		items = frappe.get_all("BOQ Item", filters={"boq_group": g.name}, fields=["planned_amount", "task"])
		amount = sum(flt(i.planned_amount) for i in items)
		total += amount
		new_tasks = sum(1 for i in items if not (i.task and frappe.db.exists("Task", i.task)))
		groups.append(
			{
				"name": g.name,
				"code": g.code,
				"group_name": g.group_name,
				"amount": amount,
				"item_count": len(items),
				"has_work_package": g.code in wp_codes,
				"new_tasks": new_tasks,
			}
		)
	return {"groups": groups, "total": total, "has_project": bool(project)}


@frappe.whitelist()
def boq_won_offer(boq: str):
	"""The bid (if any) a project would be created from — its kind, name and priced total. Drives
	the "Create project" primary action and the contract-value hint. ``None`` when no bid from this
	estimate is still waiting to become a project."""
	src = frappe.get_doc("BOQ", boq)
	if not frappe.has_permission("BOQ", "read", doc=src):
		frappe.throw(_("You are not permitted to read this BOQ."), frappe.PermissionError)
	return _won_offer_for_boq(src)


# --- assembly explosion ---------------------------------------------------


@frappe.whitelist()
def explode_item(boq_item: str):
	"""Replace a BOQ Item's sub-items with snapshot rows from its Assembly's
	components (qty = coefficient x driving_qty). Idempotent. Draft only."""
	item = frappe.get_doc("BOQ Item", boq_item)
	_require_write(item.boq)
	_require_draft(frappe.get_doc("BOQ", item.boq))
	if not item.assembly:
		frappe.throw(_("Link an Assembly to this item before exploding it."))
	assembly = frappe.get_doc("Assembly", item.assembly)
	driving = flt(item.driving_qty) or flt(item.planned_qty)

	# Preserve any caller's batch flag (import_template explodes many items).
	prior_skip = frappe.flags.get("boq_skip_rollup")
	frappe.flags.boq_skip_rollup = True
	try:
		for sub in frappe.get_all("BOQ Sub Item", filters={"boq_item": item.name}, pluck="name"):
			frappe.delete_doc("BOQ Sub Item", sub, force=True, ignore_permissions=True)
		rate = Decimal(0)
		for c in assembly.components:
			sub = frappe.get_doc(
				{
					"doctype": "BOQ Sub Item",
					"boq": item.boq,
					"boq_item": item.name,
					"rate_master": c.resource,
					"description": c.resource_name or c.resource,
					"qty_per_unit": c.coefficient,
					"qty": flt(c.coefficient) * driving,
					"work_package": item.work_package,
					"cost_head": item.cost_head,
					"source_assembly": assembly.name,
				}
			).insert(ignore_permissions=True)
			# Document.insert() leaves the object unrounded; Currency storage is
			# decimal(21,9), HALF_UP, independent of accounting rounding settings.
			rate += Decimal(str(sub.amount)).quantize(Decimal("1e-9"), rounding=ROUND_HALF_UP)
		item.quantity_source = "Assembly"
		item.rate = rate
		item.planned_qty = driving
		item.driving_qty = driving
		item.save(ignore_permissions=True)
	finally:
		frappe.flags.boq_skip_rollup = prior_skip
	recompute_boq(item.boq)
	return {"item": item.name, "sub_items": len(assembly.components)}


# --- actuals --------------------------------------------------------------


@frappe.whitelist()
def recalculate_actuals(boq: str):
	"""For items linked to a Task, set actual_qty = planned_qty x task.progress%."""
	_require_write(boq)
	frappe.flags.boq_skip_rollup = True
	try:
		for it in frappe.get_all(
			"BOQ Item",
			filters={"boq": boq, "task": ("is", "set")},
			fields=["name", "task", "planned_qty", "rate"],
		):
			progress = flt(frappe.db.get_value("Task", it.task, "progress"))
			aq = flt(it.planned_qty) * progress / 100.0
			frappe.db.set_value(
				"BOQ Item",
				it.name,
				{"actual_qty": aq, "actual_amount": aq * flt(it.rate)},
				update_modified=False,
			)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(boq)
	return True


# --- clone / revisions ----------------------------------------------------


def _clone_tree(src_boq, dst_boq, reset_actuals=True, src_wp=None, wp_override=None, drop_wp=False):
	"""Clone groups/items/sub-items from src_boq into dst_boq. `src_wp` limits the
	source to items tagged to that Work Package; `wp_override` retags the copies.
	`drop_wp` clears the Work Package on the copies (used for a cross-project clone,
	where the source project's Work Packages don't belong to the target project)."""
	group_map = {}
	for g in frappe.get_all(
		"BOQ Group", filters={"boq": src_boq}, fields=["name", "code", "group_name", "idx_order"]
	):
		ng = frappe.get_doc(
			{
				"doctype": "BOQ Group",
				"boq": dst_boq,
				"code": g.code,
				"group_name": g.group_name,
				"idx_order": g.idx_order,
			}
		).insert(ignore_permissions=True)
		group_map[g.name] = ng.name

	item_filters = {"boq": src_boq}
	if src_wp:
		item_filters["work_package"] = src_wp
	item_map = {}
	count = 0
	for it in frappe.get_all("BOQ Item", filters=item_filters, fields=["*"]):
		ni = frappe.get_doc(
			{
				"doctype": "BOQ Item",
				"boq": dst_boq,
				"boq_group": group_map.get(it.boq_group),
				"code": it.code,
				"description": it.description,
				"unit": it.unit,
				"planned_qty": it.planned_qty,
				"rate": it.rate,
				"task": None if reset_actuals else it.task,
				"quantity_source": it.quantity_source,
				"work_package": wp_override or (None if drop_wp else it.work_package),
				"cost_head": it.cost_head,
				"assembly": it.assembly,
				"driving_qty": it.driving_qty,
			}
		).insert(ignore_permissions=True)
		item_map[it.name] = ni.name
		count += 1
		for si in frappe.get_all("BOQ Sub Item", filters={"boq_item": it.name}, fields=["*"]):
			sub = frappe.get_doc(
				{
					"doctype": "BOQ Sub Item",
					"boq": dst_boq,
					"boq_item": ni.name,
					"rate_master": si.rate_master,
					"description": si.description,
					"qty_per_unit": si.qty_per_unit,
					"rate": si.rate,
					"amount": si.amount,
					"uom": si.uom,
					"qty": si.qty,
					"work_package": wp_override or (None if drop_wp else si.work_package),
					"cost_head": si.cost_head,
					"source_assembly": si.source_assembly,
				}
			)
			sub.flags.keep_snapshot = True
			sub.insert(ignore_permissions=True)
	# Drop groups that ended up empty (when filtering by a source WP).
	if src_wp:
		for gname in group_map.values():
			if not frappe.db.count("BOQ Item", {"boq_group": gname}):
				frappe.delete_doc("BOQ Group", gname, force=True, ignore_permissions=True)
	return count


def _revision_peers(doc):
	"""The BOQ names sharing a revision lineage with ``doc``.

	For a project BOQ that's every BOQ on the project. A project-less estimate isn't grouped by a
	project, so its lineage is the ``base_revision`` chain — the root plus every descendant — which
	keeps estimate revision numbers independent of other, unrelated estimates.
	"""
	if doc.get("project"):
		return frappe.get_all("BOQ", filters={"project": doc.project}, pluck="name")
	# Walk up to the lineage root, then collect every descendant by base_revision.
	root, cur, seen = doc.name, doc.name, set()
	while cur and cur not in seen:
		seen.add(cur)
		parent = frappe.db.get_value("BOQ", cur, "base_revision")
		root = parent or cur
		cur = parent
	lineage, frontier = {root}, [root]
	while frontier:
		children = frappe.get_all("BOQ", filters={"base_revision": ("in", frontier)}, pluck="name")
		frontier = [c for c in children if c not in lineage]
		lineage.update(frontier)
	return list(lineage)


@frappe.whitelist()
def create_revision(boq: str, source_sco: str | None = None, title: str | None = None):
	"""Clone a BOQ into a new Draft revision (revision = max+1, base_revision = src)."""
	if not frappe.has_permission("BOQ", "create"):
		frappe.throw(_("You are not permitted to create a BOQ."), frappe.PermissionError)
	src = frappe.get_doc("BOQ", boq)
	peers = _revision_peers(src)
	revs = frappe.get_all("BOQ", filters={"name": ("in", peers)}, pluck="revision") if peers else []
	next_rev = (max(revs) if revs else 0) + 1
	new = frappe.get_doc(
		{
			"doctype": "BOQ",
			"project": src.project,
			"title": title or f"Revision {next_rev}",
			"revision": next_rev,
			"base_revision": src.name,
			"source_sco": source_sco,
			"status": "Draft",
			"margin_rate": src.margin_rate,
			"tax_rate": src.tax_rate,
		}
	).insert(ignore_permissions=True)
	frappe.flags.boq_skip_rollup = True
	try:
		_clone_tree(src.name, new.name)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(new.name)
	return new.name


@frappe.whitelist()
def clone_boq(
	from_project: str,
	to_project: str,
	from_work_package: str | None = None,
	to_work_package: str | None = None,
	title: str | None = None,
):
	"""project->project: a new Draft BOQ on the target. wp->wp (same project):
	retag the source WP's lines onto the project's latest BOQ."""
	if not frappe.has_permission("BOQ", "create"):
		frappe.throw(_("You are not permitted to create a BOQ."), frappe.PermissionError)
	src = frappe.get_all(
		"BOQ", filters={"project": from_project}, order_by="revision desc", limit=1, pluck="name"
	)
	if not src:
		frappe.throw(_("No BOQ found on the source project."))
	src = src[0]

	if from_project == to_project:
		if not (from_work_package and to_work_package):
			frappe.throw(_("Both source and target Work Package are required for a within-project clone."))
		frappe.flags.boq_skip_rollup = True
		try:
			count = _clone_tree(
				src, src, reset_actuals=True, src_wp=from_work_package, wp_override=to_work_package
			)
		finally:
			frappe.flags.boq_skip_rollup = False
		recompute_boq(src)
		return {"mode": "wp-to-wp", "boq": src, "cloned": count}

	new = frappe.get_doc(
		{
			"doctype": "BOQ",
			"project": to_project,
			"title": title or f"Cloned from {src}",
			"status": "Draft",
		}
	).insert(ignore_permissions=True)
	frappe.flags.boq_skip_rollup = True
	try:
		count = _clone_tree(src, new.name, wp_override=to_work_package, drop_wp=True)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(new.name)
	return {"mode": "project-to-project", "boq": new.name, "cloned": count}


# --- template import ------------------------------------------------------


@frappe.whitelist()
def import_template(boq: str, estimate_template: str):
	"""Seed groups + items from an Estimate Template into a Draft BOQ. Assembly rows
	auto-explode; Resource rows get a single rate-analysis sub-item."""
	_require_write(boq)
	boq_doc = frappe.get_doc("BOQ", boq)
	_require_draft(boq_doc)
	tpl = frappe.get_doc("Estimate Template", estimate_template)

	# Reuse existing groups by name; create lazily otherwise.
	groups = {
		g.group_name: g.name
		for g in frappe.get_all("BOQ Group", filters={"boq": boq}, fields=["name", "group_name"])
	}
	seeded = 0
	frappe.flags.boq_skip_rollup = True
	try:
		for idx, row in enumerate(tpl.rows, start=1):
			gname = row.group_name or "General"
			if gname not in groups:
				code = chr(ord("A") + len(groups)) if len(groups) < 26 else f"G{len(groups) + 1}"
				ng = frappe.get_doc(
					{
						"doctype": "BOQ Group",
						"boq": boq,
						"code": code,
						"group_name": gname,
						"idx_order": len(groups) + 1,
					}
				).insert(ignore_permissions=True)
				groups[gname] = ng.name
			item = frappe.get_doc(
				{
					"doctype": "BOQ Item",
					"boq": boq,
					"boq_group": groups[gname],
					"code": f"{idx}",
					"description": row.description or (row.assembly or row.resource or "Item"),
					"unit": row.uom or "Nos",
					"planned_qty": row.placeholder_qty or 0,
					"driving_qty": row.placeholder_qty or 0,
					"rate": row.rate or 0,
					"cost_head": row.cost_head or None,
					"quantity_source": "Template",
					"assembly": row.assembly if row.line_type == "Assembly" else None,
				}
			).insert(ignore_permissions=True)
			seeded += 1
			if row.line_type == "Assembly" and row.assembly:
				explode_item(item.name)
			elif row.line_type == "Resource" and row.resource:
				frappe.get_doc(
					{
						"doctype": "BOQ Sub Item",
						"boq": boq,
						"boq_item": item.name,
						"rate_master": row.resource,
						"description": row.description or row.resource,
						"qty_per_unit": 1,
						"work_package": item.work_package,
						"cost_head": item.cost_head,
					}
				).insert(ignore_permissions=True)
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(boq)
	return {"boq": boq, "seeded": seeded}


@frappe.whitelist()
def estimate_template_preview(estimate_template: str):
	"""What an Estimate Template would seed into a BOQ: the template's description, one entry per row
	(its source + kind, group, unit, qty, rate, amount) and a total — so the import modal shows what
	gets created before it does. Mirrors import_template's row resolution."""
	if not frappe.has_permission("Estimate Template", "read"):
		frappe.throw(_("You are not permitted to read estimate templates."), frappe.PermissionError)
	tpl = frappe.get_doc("Estimate Template", estimate_template)
	rows, total = [], 0.0
	for row in tpl.rows:
		qty = flt(row.placeholder_qty)
		rate = flt(row.rate)
		amount = flt(row.amount) or qty * rate
		total += amount
		if row.line_type == "Assembly":
			kind, label = "Assembly", (row.description or row.assembly or "")
		elif row.line_type == "Resource":
			kind, label = "Rate Master", (row.description or row.resource or "")
		else:
			kind, label = "", (row.description or "")
		rows.append(
			{
				"source_label": label,
				"source_kind": kind,
				"group_name": row.group_name or "",
				"unit": row.uom or "",
				"qty": qty,
				"rate": rate,
				"amount": amount,
				"cost_head": row.cost_head or "",
			}
		)
	return {
		"template_name": tpl.template_name,
		"description": tpl.description or "",
		"row_count": len(rows),
		"total": total,
		"rows": rows,
	}


@frappe.whitelist()
def import_rows(boq: str, payload):
	"""Create groups + items on a Draft BOQ from a parsed spreadsheet import.

	``payload`` (the frontend ``buildPayload`` output) is
	``{units: [uom names to create], groups: [{key, existingId, code, name}],
	items: [{groupKey, code, description, unit, plannedQty, rate, costHead}]}``.
	The planner has already classified rows, resolved/blanked duplicate codes and listed only the
	units the user approved; this persists them. Draft only; missing UOMs are created."""
	_require_write(boq)
	boq_doc = frappe.get_doc("BOQ", boq)
	_require_draft(boq_doc)
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload

	# Units the user chose to create (the planner only lists approved ones).
	for uom in data.get("units") or []:
		uom = (uom or "").strip()
		if uom and not frappe.db.exists("UOM", uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)

	# Resolve every group key to a BOQ Group — reuse an existing one (by id or name), else create.
	existing = {
		g.group_name: g.name
		for g in frappe.get_all("BOQ Group", filters={"boq": boq}, fields=["name", "group_name"])
	}
	group_count = len(existing)
	by_key = {}
	for g in data.get("groups") or []:
		if g.get("existingId") and frappe.db.exists("BOQ Group", g["existingId"]):
			by_key[g["key"]] = g["existingId"]
			continue
		gname = (g.get("name") or "General").strip() or "General"
		if gname in existing:
			by_key[g["key"]] = existing[gname]
			continue
		code = (g.get("code") or "").strip() or (
			chr(ord("A") + group_count) if group_count < 26 else f"G{group_count + 1}"
		)
		ng = frappe.get_doc(
			{
				"doctype": "BOQ Group",
				"boq": boq,
				"code": code,
				"group_name": gname,
				"idx_order": group_count + 1,
			}
		).insert(ignore_permissions=True)
		existing[gname] = ng.name
		by_key[g["key"]] = ng.name
		group_count += 1

	# Allocate item codes: keep a provided (already-deduped) code, generate a free one otherwise.
	used = {c.lower() for c in frappe.get_all("BOQ Item", filters={"boq": boq}, pluck="code") if c}
	counter = [0]

	def _alloc(code):
		code = (code or "").strip()
		if code and code.lower() not in used:
			used.add(code.lower())
			return code
		counter[0] += 1
		while str(counter[0]) in used:
			counter[0] += 1
		used.add(str(counter[0]))
		return str(counter[0])

	seeded = 0
	frappe.flags.boq_skip_rollup = True
	try:
		for it in data.get("items") or []:
			group = by_key.get(it.get("groupKey"))
			if not group:
				continue
			qty = flt(it.get("plannedQty"))
			frappe.get_doc(
				{
					"doctype": "BOQ Item",
					"boq": boq,
					"boq_group": group,
					"code": _alloc(it.get("code")),
					"description": it.get("description") or "Item",
					"unit": it.get("unit") or "Nos",
					"planned_qty": qty,
					"driving_qty": qty,
					"rate": flt(it.get("rate")),
					"cost_head": it.get("costHead") or None,
					"quantity_source": "Manual",
				}
			).insert(ignore_permissions=True)
			seeded += 1
	finally:
		frappe.flags.boq_skip_rollup = False
	recompute_boq(boq)
	return {"boq": boq, "items": seeded, "groups": len(by_key)}


# --- work package mass assignment -----------------------------------------

_CLOSED_WP_STATUSES = ("Completed",)


def _get_doc_or_none(doctype: str, name: str | None):
	if not name:
		return None
	missing = getattr(frappe, "DoesNotExistError", LookupError)
	try:
		return frappe.get_doc(doctype, name)
	except missing:
		return None


def _raise_assignment_validation(message):
	frappe.throw(_("WP_ASSIGN_BAD_INPUT: {0}").format(message), frappe.ValidationError)


def _normalise_assignments(assignments):
	if isinstance(assignments, str):
		try:
			assignments = frappe.parse_json(assignments)
		except Exception as exc:
			_raise_assignment_validation(str(exc))

	if not isinstance(assignments, list):
		_raise_assignment_validation("assignments must be a list")

	seen = set()
	for index, row in enumerate(assignments):
		if not isinstance(row, dict):
			_raise_assignment_validation(f"line {index + 1} must be an object")
		item_name = row.get("boq_item")
		target_wp = row.get("work_package")
		if not isinstance(item_name, str) or not item_name.strip():
			_raise_assignment_validation(f"line {index + 1} boq_item must be a string")
		if not isinstance(target_wp, str) or not target_wp.strip():
			_raise_assignment_validation(f"line {index + 1} work_package must be a string")
		if item_name in seen:
			frappe.throw(
				_("WP_ASSIGN_DUPLICATE_LINE: {0}").format(item_name),
				frappe.ValidationError,
			)
		seen.add(item_name)
		reassign = row.get("reassign", 0)
		if not (isinstance(reassign, int) and reassign in (0, 1)):
			_raise_assignment_validation(f"line {index + 1} reassign must be 0, 1, or bool")
	return assignments


def _record_assignment(mapping, touched_wps, item_name, old_wp, target_wp):
	mapping[item_name] = target_wp
	for work_package in (old_wp, target_wp):
		if work_package and work_package not in touched_wps:
			touched_wps.append(work_package)


@frappe.whitelist()
def assign_work_packages(boq: str, assignments, test_run: int | bool = 1):
	"""SAP PS mass change / Fiori mass edit for BOQ Item.work_package."""
	_require_write(boq)
	assignments = _normalise_assignments(assignments)
	boq_doc = frappe.get_doc("BOQ", boq)
	existing_rows = frappe.get_list(
		"BOQ Item",
		filters={"boq": boq_doc.name},
		fields=["name", "work_package", "planned_amount"],
		limit_page_length=0,
	)
	before_wp = {row.name: row.work_package for row in existing_rows}
	amounts = {row.name: flt(row.planned_amount) for row in existing_rows}
	after_wp = dict(before_wp)

	lines = []
	valid = []
	touched_wps = []

	for row in assignments:
		item_name = row.get("boq_item")
		target_wp = row.get("work_package")
		reassign = cint(row.get("reassign", 0))
		item = _get_doc_or_none("BOQ Item", item_name)
		wp = _get_doc_or_none("Work Package", target_wp)
		current_wp = item.work_package if item else None

		if not item or item.boq != boq_doc.name:
			reason = "WP_ASSIGN_LINE_NOT_IN_BOQ"
			message = _("This BOQ line is not in BOQ {0}.").format(boq_doc.name)
		elif not frappe.has_permission("BOQ Item", "write", doc=item):
			reason = "WP_ASSIGN_NOT_PERMITTED"
			message = _("You don't have permission to change the work package on this BOQ line.")
		elif not wp or wp.project != boq_doc.project:
			reason = "WP_ASSIGN_WRONG_PROJECT"
			message = _("Work package {0} does not belong to project {1}.").format(target_wp, boq_doc.project)
		elif wp.status in _CLOSED_WP_STATUSES:
			reason = "WP_ASSIGN_WP_CLOSED"
			message = _("Work package {0} is closed and cannot receive BOQ lines.").format(target_wp)
		elif current_wp and current_wp != target_wp and not reassign:
			reason = "WP_ASSIGN_REASSIGN_REQUIRED"
			message = _("This line is already in work package {0}; confirm reassignment.").format(current_wp)
		else:
			reason = None
			message = None

		if reason:
			lines.append(
				{
					"boq_item": item_name,
					"status": "SKIPPED",
					"reason": reason,
					"message": message,
				}
			)
			continue

		valid.append(
			{
				"item": item,
				"target_wp": target_wp,
				"old_wp": current_wp,
				"line": {"boq_item": item_name, "status": "OK", "reason": None},
			}
		)
		lines.append(valid[-1]["line"])

	if cint(test_run):
		for entry in valid:
			item = entry["item"]
			_record_assignment(after_wp, touched_wps, item.name, entry["old_wp"], entry["target_wp"])
	elif valid:
		prior_skip = frappe.flags.get("boq_skip_rollup")
		frappe.flags.boq_skip_rollup = True
		saved = []
		try:
			for index, entry in enumerate(valid, start=1):
				item = entry["item"]
				target_wp = entry["target_wp"]
				savepoint = f"boq_assign_{index}"
				prior_log_len = len(frappe.local.message_log)
				frappe.db.savepoint(savepoint)
				try:
					item.work_package = target_wp
					item.save(ignore_permissions=True)
				except Exception as exc:
					frappe.db.rollback(save_point=savepoint)
					del frappe.local.message_log[prior_log_len:]
					if (
						isinstance(exc, frappe.ValidationError)
						and len(exc.args) == 1
						and isinstance(exc.args[0], str)
					):
						message = frappe.utils.strip_html_tags(str(exc))
					else:
						frappe.log_error(
							title="BOQ work package assignment",
							message=frappe.get_traceback(),
						)
						message = _(
							"Could not save this BOQ line. Please try again or contact your administrator."
						)
					entry["line"].update(
						status="SKIPPED",
						reason="WP_ASSIGN_SAVE_FAILED",
						message=message,
					)
					continue
				saved.append(entry)
				_record_assignment(after_wp, touched_wps, item.name, entry["old_wp"], target_wp)
		finally:
			frappe.flags.boq_skip_rollup = prior_skip

		if saved:
			recompute_boq(boq_doc.name)
			counts = {}
			for entry in saved:
				target_wp = entry["target_wp"]
				counts[target_wp] = counts.get(target_wp, 0) + 1
			changes = "\n".join(
				_("{0}: {1} -> {2}").format(
					entry["item"].name,
					entry["old_wp"] or _("unassigned"),
					entry["target_wp"],
				)
				for entry in saved
			)
			count_summary = ", ".join(
				_("{0}: {1}").format(work_package, count) for work_package, count in counts.items()
			)
			boq_doc.add_comment(
				"Comment",
				_("Assigned work packages:\n{0}\n\nWork Package counts: {1}").format(changes, count_summary),
			)

	packages = []
	for wp_name in touched_wps:
		before_items = [name for name, current in before_wp.items() if current == wp_name]
		after_items = [name for name, current in after_wp.items() if current == wp_name]
		packages.append(
			{
				"work_package": wp_name,
				"before_count": len(before_items),
				"after_count": len(after_items),
				"before_amount": sum(amounts.get(name, 0.0) for name in before_items),
				"after_amount": sum(amounts.get(name, 0.0) for name in after_items),
			}
		)

	ok_count = sum(line["status"] == "OK" for line in lines)
	return {
		"lines": lines,
		"packages": packages,
		"ok": ok_count,
		"skipped": len(lines) - ok_count,
	}


# --- atomic line values (Gap #6154, WP-17 Slice 1) --------------------------


def _line_values_refuse(reason, permission=False):
	frappe.throw(reason, frappe.PermissionError if permission else frappe.ValidationError)


def _line_request_id(request_id, required=True):
	if request_id is None and not required:
		return None
	if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
		_line_values_refuse("BOQ_LINES_INVALID")
	return request_id


def _line_changes(changes):
	if isinstance(changes, str):
		changes = frappe.parse_json(changes)
	if not isinstance(changes, list) or not changes:
		_line_values_refuse("BOQ_LINES_INVALID")
	if len(changes) > 1000:
		_line_values_refuse("BOQ_LINES_TOO_MANY")
	shapes = (
		{"boq_item", "rate"},
		{"boq_item", "planned_qty"},
		{"boq_item", "rate_change_percent"},
		{"boq_item", "rate", "assembly"},
	)
	seen = set()
	for change in changes:
		if not isinstance(change, dict) or set(change) not in shapes:
			_line_values_refuse("BOQ_LINES_INVALID")
		name = change["boq_item"]
		if not isinstance(name, str) or not name or name in seen:
			_line_values_refuse("BOQ_LINES_INVALID")
		if "assembly" in change and (not isinstance(change["assembly"], str) or not change["assembly"]):
			_line_values_refuse("BOQ_LINES_INVALID")
		seen.add(name)
	return sorted(changes, key=lambda row: row["boq_item"])


def _line_decimal(value):
	try:
		if isinstance(value, bool) or value is None:
			raise InvalidOperation
		number = Decimal(str(value))
		# Frappe's numeric columns are decimal(21,9); refuse unrepresentable input.
		if not number.is_finite() or abs(number) >= Decimal("1e12"):
			raise InvalidOperation
		return number
	except (InvalidOperation, ValueError, TypeError) as _exc:
		_line_values_refuse("BOQ_LINE_VALUE_INVALID")


def _line_round(value, doc, field):
	precision = doc.precision(field)
	if precision is None:
		_line_values_refuse("BOQ_LINE_PRECISION_UNAVAILABLE")
	return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def _line_values(item):
	values = {"assembly": item.get("assembly") or None}
	for field in ("rate", "planned_qty", "planned_amount"):
		stored = _line_decimal(item.get(field) or 0)
		rounded = _line_round(stored, item, field)
		# Snapshot inputs can exceed display precision; do not misrepresent them.
		values[field] = str(stored if field != "planned_amount" and stored != rounded else rounded)
	return values


def _line_current_revision(doc):
	if doc.status != "Draft" or doc.get("docstatus", 0) != 0:
		_line_values_refuse("BOQ_NOT_CURRENT_REVISION")
	# The first tender Draft may have no base. Once an Approved revision exists,
	# an unscoped Draft is not an eligible replacement for it.
	others = frappe.get_all(
		"BOQ", filters={"project": doc.project}, fields=["name", "status", "base_revision", "company"]
	)
	if any(row.name != doc.name and row.status in ("Draft", "Submitted") for row in others):
		_line_values_refuse("BOQ_NOT_CURRENT_REVISION")
	approved = [row for row in others if row.status == "Approved"]
	if doc.base_revision:
		if len(approved) != 1 or approved[0].name != doc.base_revision or approved[0].company != doc.company:
			_line_values_refuse("BOQ_NOT_CURRENT_REVISION")
	elif approved:
		_line_values_refuse("BOQ_NOT_CURRENT_REVISION")


def _line_state(doc, items):
	currency = frappe.db.get_value("Company", doc.company, "default_currency")
	if not currency:
		_line_values_refuse("BOQ_LINE_CURRENCY_UNAVAILABLE")
	state = {
		"boq": {
			key: doc.get(key)
			for key in (
				"name",
				"status",
				"docstatus",
				"revision",
				"base_revision",
				"owner",
				"modified",
				"company",
				"project",
				"margin_rate",
				"tax_rate",
			)
		},
		"currency": currency,
		"precision": {
			key: doc.precision(key) for key in ("planned_amount", "margin_amount", "tax_amount", "total")
		},
		"items": [],
	}
	for item in items:
		state["items"].append(
			{
				**{
					key: item.get(key)
					for key in (
						"name",
						"modified",
						"rate",
						"planned_qty",
						"planned_amount",
						"assembly",
						"boq_group",
					)
				},
				"precision": {key: item.precision(key) for key in ("rate", "planned_qty", "planned_amount")},
				"sub_items": frappe.db.count("BOQ Sub Item", {"boq_item": item.name}),
			}
		)
	return state


def _line_hash(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


def _line_canonical(changes):
	canonical = []
	for row in changes:
		canonical_row = {}
		for key, value in row.items():
			try:
				canonical_row[key] = (
					str(_line_decimal(value).normalize()) if key not in ("boq_item", "assembly") else value
				)
			except frappe.ValidationError:
				canonical_row[key] = str(value)
		canonical.append(canonical_row)
	return canonical


def _line_fields(change):
	return ["rate" if key == "rate_change_percent" else key for key in change if key != "boq_item"]


def _line_can_write(item, change):
	return frappe.has_permission("BOQ Item", "write", doc=item) and all(
		item.has_permlevel_access_to(field, permission_type="write") for field in _line_fields(change)
	)


def _line_outcome_signature(payload):
	# Reuse native site-local signing, so a user-authored/edited Comment cannot
	# fabricate success. No staging records, secret storage or new dependency.
	from frappe.utils.verified_command import get_signed_params

	return get_signed_params(_line_hash(payload))


def _line_items(doc, locked=None):
	# Read all lines, not a permission-filtered subset: partial totals are unsafe.
	# Explicit BOQ and line permission checks precede disclosure to the caller.
	locked = locked or {}
	names = frappe.get_all("BOQ Item", filters={"boq": doc.name}, pluck="name")
	items = [locked.get(name) or frappe.get_doc("BOQ Item", name) for name in sorted(names)]
	for item in items:
		if not frappe.has_permission("BOQ Item", "read", doc=item) or any(
			not item.has_permlevel_access_to(field, permission_type="read")
			for field in (
				"rate",
				"planned_qty",
				"planned_amount",
				"assembly",
				"code",
				"description",
				"boq_group",
			)
		):
			_line_values_refuse("BOQ_LINE_FIELD_NOT_PERMITTED", permission=True)
	return items


def _line_totals(doc, planned):
	margin = planned * _line_decimal(doc.margin_rate or 0) / Decimal(100)
	tax = (planned + margin) * _line_decimal(doc.tax_rate or 0) / Decimal(100)
	return {
		key: str(_line_round(value, doc, key))
		for key, value in {
			"planned_amount": planned,
			"margin_amount": margin,
			"tax_amount": tax,
			"total": planned + margin + tax,
		}.items()
	}


def _line_preview(doc, changes, locked=None):
	_line_current_revision(doc)
	_require_write(doc)
	items = _line_items(doc, locked)
	by_name = {item.name: item for item in items}
	state = _line_state(doc, items)
	result = {
		"boq": doc.name,
		"currency": state["currency"],
		"owner": doc.owner,
		"owner_ack_required": doc.owner != frappe.session.user,
		# Two hashes let outcome recovery compare the snapshot without storing previews.
		"fingerprint": _line_hash(state) + "." + _line_hash(_line_canonical(changes)),
		"lines": [],
		"groups": [],
	}
	after_amounts = {item.name: _line_decimal(item.planned_amount or 0) for item in items}
	for change in changes:
		item = by_name.get(change["boq_item"])
		if not item or item.boq != doc.name:
			_line_values_refuse("BOQ_LINE_NOT_IN_BOQ")
		before = _line_values(item)
		after = dict(before)
		stored_amount = _line_decimal(item.planned_amount or 0)
		reason = None
		fields = _line_fields(change)
		if not _line_can_write(item, change):
			reason = "BOQ_LINE_FIELD_NOT_PERMITTED"
		elif "rate" in fields and (item.assembly or frappe.db.count("BOQ Sub Item", {"boq_item": item.name})):
			reason = "BOQ_LINE_RATE_FROM_BUILDUP"
		else:
			try:
				for field in fields:
					if field == "assembly":
						if not frappe.db.exists("Assembly", change[field]):
							_line_values_refuse("BOQ_LINE_ASSEMBLY_NOT_FOUND")
						assembly = frappe.get_doc("Assembly", change[field])
						if not frappe.has_permission("Assembly", "read", doc=assembly):
							_line_values_refuse("BOQ_LINE_FIELD_NOT_PERMITTED")
						if assembly.get("disabled"):
							_line_values_refuse("BOQ_LINE_ASSEMBLY_DISABLED")
						after[field] = change[field]
						continue
					if "rate_change_percent" in change:
						value = _line_decimal(item.rate or 0) * (
							Decimal(1) + _line_decimal(change["rate_change_percent"]) / Decimal(100)
						)
					else:
						value = _line_decimal(change[field])
					rounded = _line_round(value, item, field)
					if value < 0 or abs(value) >= Decimal("1e12"):
						_line_values_refuse("BOQ_LINE_VALUE_INVALID")
					if "rate_change_percent" not in change and value != rounded:
						_line_values_refuse("BOQ_LINE_PRECISION_EXCEEDED")
					after[field] = str(rounded)
				# Mirror BOQItem.validate's float product and decimal(21,9) storage,
				# including binary half-cent drift; keep untouched snapshot inputs.
				rate = after["rate"] if "rate" in fields else item.rate
				qty = after["planned_qty"] if "planned_qty" in fields else item.planned_qty
				stored_amount = _line_decimal(flt(qty) * flt(rate)).quantize(
					Decimal("1e-9"), rounding=ROUND_HALF_UP
				)
				after["planned_amount"] = str(_line_round(stored_amount, item, "planned_amount"))
			except frappe.ValidationError as exc:
				reason = str(exc)
		if reason:
			after = dict(before)
			stored_amount = _line_decimal(item.planned_amount or 0)
		after_amounts[item.name] = stored_amount
		result["lines"].append(
			{
				"boq_item": item.name,
				"code": item.code,
				"description": item.description,
				"precision": {
					field: item.precision(field) for field in ("rate", "planned_qty", "planned_amount")
				},
				"before": before,
				"after": after,
				"delta": str(
					_line_round(
						_line_decimal(after["planned_amount"]) - _line_decimal(before["planned_amount"]),
						item,
						"planned_amount",
					)
				),
				"status": "REFUSED" if reason else "OK",
				"reason": reason,
			}
		)
	for group in sorted({item.boq_group for item in items}):
		before = sum(
			(_line_decimal(item.planned_amount or 0) for item in items if item.boq_group == group), Decimal(0)
		)
		after = sum((after_amounts[item.name] for item in items if item.boq_group == group), Decimal(0))
		result["groups"].append(
			{
				"boq_group": group,
				"before": str(_line_round(before, doc, "planned_amount")),
				"after": str(_line_round(after, doc, "planned_amount")),
				"delta": str(_line_round(after - before, doc, "planned_amount")),
			}
		)
	before = _line_totals(doc, sum((_line_decimal(item.planned_amount or 0) for item in items), Decimal(0)))
	after = _line_totals(doc, sum(after_amounts.values(), Decimal(0)))
	result["totals"] = {
		"before": before,
		"after": after,
		"delta": str(_line_decimal(after["total"]) - _line_decimal(before["total"])),
	}
	return result


def _line_outcome(doc, request_id):
	for comment in frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "BOQ",
			"reference_name": doc.name,
			"comment_type": "Comment",
			"content": ("like", "%BOQLineValues.v1%"),
		},
		fields=["content"],
	):
		try:
			payload = json.loads(html.unescape(re.sub(r"<[^>]*>", "", comment.content)))
			if not isinstance(payload, dict):
				continue
			signature = payload.pop("signature", None)
			if not isinstance(signature, str) or not hmac.compare_digest(
				signature, _line_outcome_signature(payload)
			):
				continue
		except (ValueError, TypeError) as _exc:
			continue
		if (
			payload.get("contract") == "BOQLineValues.v1"
			and payload.get("request_id") == request_id
			and payload.get("outcome", {}).get("boq") == doc.name
		):
			return payload["outcome"]
	return None


@frappe.whitelist()
def preview_update_line_values(boq, changes, request_id=None):
	"""Read-only, decimal-valued simulation; never saves even temporarily."""
	_line_request_id(request_id, required=False)
	changes = _line_changes(changes)
	doc = frappe.get_doc("BOQ", boq)
	return _line_preview(doc, changes)


@frappe.whitelist(methods=["POST"])
def update_line_values(boq, changes, fingerprint, request_id, test_run=1, edit_other_draft=0):
	"""All-or-nothing native saves. Caller/worker owns the outer transaction."""
	_line_request_id(request_id)
	changes = _line_changes(changes)
	if test_run not in (0, 1, "0", "1") or edit_other_draft not in (0, 1, "0", "1"):
		_line_values_refuse("BOQ_LINES_INVALID")
	if cint(test_run):
		return preview_update_line_values(boq, changes, request_id)
	doc = frappe.get_doc("BOQ", boq)
	_require_write(doc)
	# Refuse unauthorized/foreign/missing lines before taking any row lock.
	names = set(frappe.get_all("BOQ Item", filters={"boq": doc.name}, pluck="name"))
	for change in changes:
		if change["boq_item"] not in names:
			_line_values_refuse("BOQ_LINE_NOT_IN_BOQ")
		item = frappe.get_doc("BOQ Item", change["boq_item"])
		if not _line_can_write(item, change):
			_line_values_refuse("BOQ_LINE_FIELD_NOT_PERMITTED", permission=True)
	doc = frappe.get_doc("BOQ", boq, for_update=True)
	_require_write(doc)
	locked = {
		row["boq_item"]: frappe.get_doc("BOQ Item", row["boq_item"], for_update=True) for row in changes
	}
	stored = _line_outcome(doc, request_id)
	if stored:
		for change in changes:
			item = locked[change["boq_item"]]
			if item.boq != doc.name:
				_line_values_refuse("BOQ_LINE_NOT_IN_BOQ")
			if not _line_can_write(item, change):
				_line_values_refuse("BOQ_LINE_FIELD_NOT_PERMITTED", permission=True)
		if stored["fingerprint"] != fingerprint or fingerprint.split(".")[-1] != _line_hash(
			_line_canonical(changes)
		):
			_line_values_refuse("BOQ_LINES_STALE")
		return stored
	result = _line_preview(doc, changes, locked)
	if result["owner_ack_required"] and not cint(edit_other_draft):
		_line_values_refuse("BOQ_DRAFT_OWNER_ACK_REQUIRED")
	for line in result["lines"]:
		if line["reason"]:
			_line_values_refuse(line["reason"], permission=line["reason"] == "BOQ_LINE_FIELD_NOT_PERMITTED")
	if not fingerprint or fingerprint != result["fingerprint"]:
		_line_values_refuse("BOQ_LINES_STALE")
	prior_skip = frappe.flags.get("boq_skip_rollup")
	if prior_skip:
		_line_values_refuse("BOQ_LINES_ROLLUP_SUSPENDED")
	savepoint = "boq_line_values"
	frappe.db.savepoint(savepoint)
	try:
		frappe.flags.boq_skip_rollup = True
		for line, change in zip(result["lines"], changes, strict=True):
			item = locked[line["boq_item"]]
			for field in _line_fields(change):
				setattr(item, field, line["after"][field])
			item.save()
		frappe.flags.boq_skip_rollup = prior_skip
		recompute_boq(doc.name)
		for line in result["lines"]:
			saved = frappe.get_doc("BOQ Item", line["boq_item"], for_update=True)
			if _line_values(saved) != line["after"]:
				_line_values_refuse("BOQ_LINE_SAVED_VALUES_MISMATCH")
		saved_boq = frappe.get_doc("BOQ", doc.name, for_update=True)
		if any(
			str(_line_round(_line_decimal(saved_boq.get(field) or 0), saved_boq, field)) != value
			for field, value in result["totals"]["after"].items()
		):
			_line_values_refuse("BOQ_LINE_SAVED_VALUES_MISMATCH")
		result.update(
			status="COMMITTED",
			request_id=request_id,
			line_count=len(changes),
			actor=frappe.session.user,
			edit_other_draft=cint(edit_other_draft),
		)
		# BOQ timeline readers need not have access to every line/permlevel.
		# Store only the designed outcome summary, never per-line field values.
		result.pop("lines")
		result.pop("groups")
		payload = {
			"contract": "BOQLineValues.v1",
			"request_id": request_id,
			"outcome": result,
		}
		payload["signature"] = _line_outcome_signature(payload)
		doc.add_comment("Comment", "<pre>" + html.escape(json.dumps(payload, sort_keys=True)) + "</pre>")
		return result
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	finally:
		frappe.flags.boq_skip_rollup = prior_skip


@frappe.whitelist()
def get_line_values_outcome(boq, request_id, fingerprint=None):
	"""Only the transactional BOQ Comment proves commit, never an RQ state."""
	_line_request_id(request_id)
	doc = frappe.get_doc("BOQ", boq)
	if not frappe.has_permission("BOQ", "read", doc=doc):
		_line_values_refuse("BOQ_LINE_FIELD_NOT_PERMITTED", permission=True)
	stored = _line_outcome(doc, request_id)
	if stored:
		return stored
	state_hash = _line_hash(_line_state(doc, _line_items(doc)))
	safe = isinstance(fingerprint, str) and fingerprint.split(".")[0] == state_hash
	return {
		"boq": boq,
		"request_id": request_id,
		"status": "NOT_APPLIED_SAFE_RETRY" if safe else "STALE_REPREVIEW",
	}
