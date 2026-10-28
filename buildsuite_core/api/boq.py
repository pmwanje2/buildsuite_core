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


@frappe.whitelist()
def create_revision(boq: str, source_sco: str | None = None, title: str | None = None):
	"""Clone a BOQ into a new Draft revision (revision = max+1, base_revision = src)."""
	if not frappe.has_permission("BOQ", "create"):
		frappe.throw(_("You are not permitted to create a BOQ."), frappe.PermissionError)
	src = frappe.get_doc("BOQ", boq)
	revs = frappe.get_all("BOQ", filters={"project": src.project}, pluck="revision")
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
	except InvalidOperation, ValueError, TypeError:
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
		except ValueError, TypeError:
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
