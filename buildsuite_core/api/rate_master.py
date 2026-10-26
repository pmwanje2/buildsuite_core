# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt


@frappe.whitelist()
def list_rate_masters(start: int = 0, page_length: int = 10, search: str | None = None, category: str | None = None, with_counts: bool = False):
	"""One page of rate masters + total_count (filtered, for the pager). When
	with_counts is set, also the global total and per-category counts (KPI cards)."""
	# Rate masters are a shared, company-agnostic catalog — no company scoping.
	filters = {}
	if category:
		filters["category"] = category
	or_filters = None
	if search:
		or_filters = [
			["rate_code", "like", f"%{search}%"],
			["rate_name", "like", f"%{search}%"],
		]

	rows = frappe.get_list(
		"Construction Rate Master",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name", "rate_code", "rate_name", "category", "uom",
			"current_rate", "previous_rate", "effective_date", "modified_by",
		],
		order_by="modified desc",
		start=cint(start),
		page_length=cint(page_length),
	)
	total_count = frappe.get_list(
		"Construction Rate Master",
		filters=filters,
		or_filters=or_filters,
		fields=[{"COUNT": "name", "as": "count"}],
	)[0].count

	result = {"rows": rows, "total_count": total_count}

	# Global counts (whole catalog) change only on add/edit/delete, not on paging or
	# search — so the client asks for them once on load + after mutations, not per page.
	if cint(with_counts):
		cat_rows = frappe.get_list(
			"Construction Rate Master",
			fields=["category", {"COUNT": "name", "as": "count"}],
			group_by="category",
		)
		result["total"] = sum(r.count for r in cat_rows)
		result["category_counts"] = {r.category: r.count for r in cat_rows}

	return result


@frappe.whitelist()
def get_rate_update_threshold():
	"""PO-submit rate-prompt threshold (%) from Core Settings; 5.0 if unset."""
	raw = frappe.db.get_single_value("BuildSuite Core Settings", "rate_master_update_threshold")
	return flt(raw) if raw else 5.0


@frappe.whitelist()
def update_rates_from_po(purchase_order: str, updates: str, supplier: str | None = None):
	"""Retained only to give stale clients a safe, actionable refusal."""
	frappe.throw(
		_("Purchase orders no longer change estimating rates. Use Price changes to review."),
		title="RATE_PO_PATH_RETIRED",
	)
