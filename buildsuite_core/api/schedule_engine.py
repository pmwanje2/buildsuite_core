"""Authoritative scheduling engine: conflict flagging + downstream cascade.

This is the server-side mirror of the frontend `useScheduleEngine` composable. The
algorithm is PURE (operates on plain task/edge dicts) so the two implementations
stay line-for-line in sync and are pinned by shared test vectors. The client runs
the same logic for instant Gantt feedback; the server is authoritative — it stores
the conflict flags (so Desk/reports/other users see them) and commits cascades.

Dependency model (ERPNext-native): edges live on Task.depends_on, whose `task`
field is the PREDECESSOR, enriched with custom fields dependency_type (FS/SS/FF)
and lag_days. Successors are inferred by reversing the edge set. Calendar days
only (no working-day calendar). Milestones are single-date nodes (exp_end_date is
the due date; exp_start_date stays empty).
"""

import math
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

VALID_DEP_TYPES = ("FS", "SS", "FF")
RESCHEDULABLE_STATUSES = ("Open", "Working", "Overdue")


def _is_reschedulable_status(status):
	return status in RESCHEDULABLE_STATUSES


# --- pure date helpers (mirror _scheduleAddDays / _scheduleDiffDays) -----------


def _add_days(d, days):
	if not d:
		return None
	return getdate(d) + timedelta(days=int(days or 0))


def _diff_days(frm, to):
	# Calendar-day delta (to - frm); positive when to > frm.
	if not frm or not to:
		return 0
	return (getdate(to) - getdate(frm)).days


def _iso(d):
	return d.isoformat() if d else None


# --- pure graph algorithm (mirror the prototype engine) -----------------------


def _earliest_start_for_edge(pred, succ, dep, succ_duration_override=None):
	"""Earliest allowed start date for `succ` given one predecessor edge `dep`.

	Milestone predecessor: every dep type collapses to "milestone date + lag"
	(no span). Milestone successor: duration = 0.
	"""
	if not pred or not succ or not dep:
		return None
	lag = int(dep.get("lag") or 0)
	dtype = dep.get("type")
	pred_is_ms = pred.get("type") == "Milestone"
	succ_is_ms = succ.get("type") == "Milestone"
	if succ_is_ms:
		succ_dur = 0
	elif succ_duration_override is not None:
		succ_dur = succ_duration_override
	else:
		succ_dur = _diff_days(succ.get("start"), succ.get("end"))

	if dtype == "FS":
		if not pred.get("end"):
			return None
		return _add_days(pred.get("end"), lag)
	if dtype == "SS":
		anchor = pred.get("end") if pred_is_ms else pred.get("start")
		if not anchor:
			return None
		return _add_days(anchor, lag)
	if dtype == "FF":
		if not pred.get("end"):
			return None
		earliest_succ_end = _add_days(pred.get("end"), lag)
		return _add_days(earliest_succ_end, -succ_dur)
	return None


def _compute_earliest_start(task_id, tasks_by_id, edges):
	"""MAX (most binding) earliest-start over all predecessor edges.
	Returns (date, reason) or (None, None) when unconstrained."""
	succ = tasks_by_id.get(task_id)
	if not succ:
		return None, None
	best_date = None
	best_reason = None
	for dep in edges:
		if dep["successor"] != task_id:
			continue
		pred = tasks_by_id.get(dep["predecessor"])
		if not pred:
			continue
		earliest = _earliest_start_for_edge(pred, succ, dep)
		if not earliest:
			continue
		if best_date is None or _diff_days(best_date, earliest) > 0:
			best_date = earliest
			best_reason = f"{dep['type']}, lag {dep.get('lag') or 0} from {dep['predecessor']}"
	return best_date, best_reason


def _downstream_subgraph(root_id, edges):
	subgraph = {root_id}
	stack = [root_id]
	while stack:
		node = stack.pop()
		for e in edges:
			if e["predecessor"] == node and e["successor"] not in subgraph:
				subgraph.add(e["successor"])
				stack.append(e["successor"])
	return subgraph


def _topological_downstream(root_id, edges):
	"""[root, ...successors] in topological order. If a cycle is present the
	returned order is SHORTER than the subgraph (caller aborts)."""
	subgraph = _downstream_subgraph(root_id, edges)
	indeg = {i: 0 for i in subgraph}
	for d in edges:
		if d["predecessor"] in subgraph and d["successor"] in subgraph:
			indeg[d["successor"]] = indeg.get(d["successor"], 0) + 1
	queue = [i for i, deg in indeg.items() if deg == 0]
	order = []
	while queue:
		node = queue.pop(0)
		order.append(node)
		for e in edges:
			if e["predecessor"] == node and e["successor"] in subgraph:
				indeg[e["successor"]] -= 1
				if indeg[e["successor"]] == 0:
					queue.append(e["successor"])
	return order, len(subgraph)


def compute_conflicts(root_id, tasks_by_id, edges):
	"""FLAG-ONLY: {task_id: (conflict_bool, reason)} for the downstream subgraph.
	Never moves dates. Milestone anchor = end; Activity/Inspection anchor = start.
	Inspection gate: a predecessor Inspection not Completed flags the successor."""
	result = {}
	for tid in _downstream_subgraph(root_id, edges):
		task = tasks_by_id.get(tid)
		if not task:
			continue
		is_ms = task.get("type") == "Milestone"
		anchor = task.get("end") if is_ms else task.get("start")
		earliest, reason = _compute_earliest_start(tid, tasks_by_id, edges)
		conflict = False
		conflict_reason = ""
		if earliest and anchor and _diff_days(anchor, earliest) > 0:
			conflict = True
			days = _diff_days(anchor, earliest)
			which = "Due" if is_ms else "Starts"
			conflict_reason = f"{which} {days} day{'' if days == 1 else 's'} earlier than allowed ({reason})."

		incomplete_inspection = None
		for d in edges:
			if d["successor"] != tid:
				continue
			pred = tasks_by_id.get(d["predecessor"])
			if pred and pred.get("type") == "Inspection" and pred.get("status") != "Completed":
				incomplete_inspection = pred
				break
		if incomplete_inspection:
			conflict = True
			ins_reason = f"Waiting on inspection {incomplete_inspection['name']} ({incomplete_inspection.get('status')})."
			conflict_reason = f"{ins_reason} {conflict_reason}" if conflict_reason else ins_reason

		result[tid] = (conflict, conflict_reason)
	return result


def compute_cascade(root_id, tasks_by_id, edges, root_override=None, asap=False):
	"""CASCADE: duration-preserving relationship shifts in topological order.
	Forward shifts preserve the existing behavior. With ``asap=True``, eligible
	open/overdue tasks without an actual start may also pull back to their binding
	earliest date. ``root_override`` sets the root's working dates (the user's
	move) without persisting; when it is absent, ASAP also aligns the root against
	its predecessors. Returns moves[] or None on a cycle."""
	order, size = _topological_downstream(root_id, edges)
	if len(order) != size:
		return None  # cycle

	working = {}
	for tid in order:
		t = tasks_by_id.get(tid)
		if not t:
			continue
		is_ms = t.get("type") == "Milestone"
		start = t.get("start")
		end = t.get("end")
		if tid == root_id and root_override:
			start = root_override.get("start")
			end = root_override.get("end")
		working[tid] = {
			"start": start,
			"end": end,
			"dur": 0 if is_ms else _diff_days(start, end),
			"is_ms": is_ms,
			"type": t.get("type"),
			"act_start_date": t.get("act_start_date"),
		}

	moves = []
	for tid in order:
		if tid == root_id and not (asap and root_override is None):
			continue
		task = tasks_by_id.get(tid)
		if not task:
			continue
		if tid == root_id:
			if task.get("status") not in ("Open", "Overdue") or task.get("act_start_date"):
				continue
		elif not _is_reschedulable_status(task.get("status")):
			continue
		cur = working.get(tid)
		if not cur:
			continue
		if cur["is_ms"]:
			if not cur["end"]:
				continue
		elif not cur["start"] or not cur["end"]:
			continue

		binding = None
		binding_reason = None
		for dep in edges:
			if dep["successor"] != tid:
				continue
			pd = working.get(dep["predecessor"])
			if pd is None:
				t = tasks_by_id.get(dep["predecessor"])
				pd = {"start": t.get("start"), "end": t.get("end"), "type": t.get("type")} if t else None
			if not pd:
				continue
			pred = {"start": pd.get("start"), "end": pd.get("end"), "type": pd.get("type")}
			if dep["type"] == "SS" and pred.get("type") != "Milestone":
				if not pd.get("start"):
					continue
			elif not pd.get("end"):
				continue
			succ = {"start": cur["start"], "end": cur["end"], "type": cur["type"]}
			earliest = _earliest_start_for_edge(pred, succ, dep, cur["dur"])
			if not earliest:
				continue
			if binding is None or _diff_days(binding, earliest) > 0:
				binding = earliest
				binding_reason = f"{dep['type']}, lag {dep.get('lag') or 0} from {dep['predecessor']}"

		if not binding:
			continue
		anchor = cur["end"] if cur["is_ms"] else cur["start"]
		delta = _diff_days(anchor, binding)
		if delta > 0:
			if cur["is_ms"]:
				moves.append(
					{
						"task": tid,
						"old_start": None,
						"old_end": _iso(cur["end"]),
						"new_start": None,
						"new_end": _iso(binding),
						"reason": binding_reason,
						"is_milestone": True,
					}
				)
				working[tid] = {**cur, "end": binding}
			else:
				new_end = _add_days(binding, cur["dur"])
				moves.append(
					{
						"task": tid,
						"old_start": _iso(cur["start"]),
						"old_end": _iso(cur["end"]),
						"new_start": _iso(binding),
						"new_end": _iso(new_end),
						"reason": binding_reason,
						"is_milestone": False,
					}
				)
				working[tid] = {**cur, "start": binding, "end": new_end}
		elif (
			asap
			and delta < 0
			and task.get("status") in ("Open", "Overdue")
			and not task.get("act_start_date")
		):
			if cur["is_ms"]:
				moves.append(
					{
						"task": tid,
						"old_start": None,
						"old_end": _iso(cur["end"]),
						"new_start": None,
						"new_end": _iso(binding),
						"reason": binding_reason,
						"is_milestone": True,
					}
				)
				working[tid] = {**cur, "end": binding}
			else:
				new_end = _add_days(binding, cur["dur"])
				moves.append(
					{
						"task": tid,
						"old_start": _iso(cur["start"]),
						"old_end": _iso(cur["end"]),
						"new_start": _iso(binding),
						"new_end": _iso(new_end),
						"reason": binding_reason,
						"is_milestone": False,
					}
				)
				working[tid] = {**cur, "start": binding, "end": new_end}
	return moves


def compute_cpm(tasks, edges, data_date, finish_constraint=None):
	"""Pure Critical Path Method (forward + backward pass) from ``data_date``.

	Follows the Oracle P6 'retained logic' default:
	- Open tasks with NO predecessors start at ``max(data_date, planned start)``
	  (planned start acts as a start-no-earlier-than for unconstrained activities).
	- Open and in-progress tasks WITH predecessors start at
	  ``max(predecessor-driven date, data_date)`` and ignore planned start.
	- In-progress tasks use ``math.ceil(planned_dur * (100 - progress) / 100)``
	  for remaining duration.
	- Completed tasks are fixed to actuals (falling back to planned dates), are
	  never critical, and report ``total_float=None`` and ``free_float=None``.
	- Group/summary tasks (``is_group=1``) and ``Cancelled`` tasks are excluded
	  from the CPM network and edges touching them are dropped.
	- Tasks missing required dates are excluded and returned in ``skipped``.
	"""
	if isinstance(tasks, dict):
		raw_tasks = dict(tasks)
	else:
		raw_tasks = {t["name"]: t for t in (tasks or []) if t.get("name")}

	tasks_by_id = {}
	skipped = []
	for tid, t in raw_tasks.items():
		if bool(t.get("is_group")) or t.get("status") == "Cancelled":
			continue
		is_ms = t.get("type") == "Milestone"
		start = getdate(t["start"]) if t.get("start") else None
		end = getdate(t["end"]) if t.get("end") else None
		act_start = getdate(t["act_start_date"]) if t.get("act_start_date") else None
		act_end = getdate(t["act_end_date"]) if t.get("act_end_date") else None
		progress = min(max(flt(t.get("progress") or 0), 0.0), 100.0)
		status = t.get("status") or "Open"
		is_completed = status == "Completed" or progress >= 100.0
		if is_ms:
			has_dates = bool((act_end or act_start or end or start) if is_completed else end)
		else:
			has_dates = (
				bool((act_start or start) and (act_end or end)) if is_completed else bool(start and end)
			)
		if not has_dates:
			skipped.append(tid)
			continue
		tasks_by_id[tid] = t

	if not tasks_by_id:
		return {"tasks": [], "skipped": skipped}

	base_date = getdate(data_date) if data_date else getdate(today())
	constraint_date = getdate(finish_constraint) if finish_constraint else None

	valid_edges = [
		{
			"predecessor": e["predecessor"],
			"successor": e["successor"],
			"type": e.get("type") or "FS",
			"lag": int(e.get("lag") or 0),
		}
		for e in (edges or [])
		if e.get("predecessor") in tasks_by_id and e.get("successor") in tasks_by_id
	]

	indeg = {tid: 0 for tid in tasks_by_id}
	preds_by_succ = {tid: [] for tid in tasks_by_id}
	succs_by_pred = {tid: [] for tid in tasks_by_id}
	for edge in valid_edges:
		indeg[edge["successor"]] += 1
		preds_by_succ[edge["successor"]].append(edge)
		succs_by_pred[edge["predecessor"]].append(edge)

	queue = [tid for tid, deg in indeg.items() if deg == 0]
	order = []
	while queue:
		node = queue.pop(0)
		order.append(node)
		for edge in succs_by_pred[node]:
			succ = edge["successor"]
			indeg[succ] -= 1
			if indeg[succ] == 0:
				queue.append(succ)

	if len(order) != len(tasks_by_id):
		frappe.throw(
			_("SCHEDULE_CYCLE: Circular dependency detected in schedule."),
			frappe.ValidationError,
		)

	early = {}
	for tid in order:
		t = tasks_by_id[tid]
		is_ms = t.get("type") == "Milestone"
		start = getdate(t["start"]) if t.get("start") else None
		end = getdate(t["end"]) if t.get("end") else None
		act_start = getdate(t["act_start_date"]) if t.get("act_start_date") else None
		act_end = getdate(t["act_end_date"]) if t.get("act_end_date") else None
		progress = min(max(flt(t.get("progress") or 0), 0.0), 100.0)
		status = t.get("status") or "Open"
		is_completed = status == "Completed" or progress >= 100.0
		is_in_progress = not is_completed and (status == "Working" or progress > 0.0 or act_start is not None)
		planned_dur = 0 if is_ms else max(0, _diff_days(start, end))
		node_type = "Milestone" if is_ms else (t.get("type") or "Activity")

		if is_completed:
			if is_ms:
				ms_date = act_end or act_start or end or start
				es = ms_date
				ef = ms_date
				dur = 0
			else:
				es = act_start or start
				ef = act_end or end
				if _diff_days(es, ef) < 0:
					ef = es
				dur = _diff_days(es, ef)
		else:
			if is_ms:
				dur = 0
			elif is_in_progress:
				dur = math.ceil(planned_dur * (100.0 - progress) / 100.0)
			else:
				dur = planned_dur

			if preds_by_succ[tid] or is_in_progress:
				es = base_date
			elif is_ms:
				es = max(base_date, end)
			else:
				es = max(base_date, start)

			succ_proxy = {
				"start": es,
				"end": _add_days(es, dur),
				"type": node_type,
			}
			for dep in preds_by_succ[tid]:
				pred_node = early[dep["predecessor"]]
				pred_proxy = {
					"start": pred_node["es"],
					"end": pred_node["ef"],
					"type": pred_node["type"],
				}
				cand = _earliest_start_for_edge(pred_proxy, succ_proxy, dep, dur)
				if cand and _diff_days(es, cand) > 0:
					es = cand
			ef = _add_days(es, dur)

		early[tid] = {
			"es": es,
			"ef": ef,
			"dur": dur,
			"is_ms": is_ms,
			"is_completed": is_completed,
			"type": node_type,
		}

	open_efs = [node["ef"] for node in early.values() if not node["is_completed"]]
	project_lf = (
		constraint_date
		if constraint_date is not None
		else (max(open_efs) if open_efs else max((node["ef"] for node in early.values()), default=base_date))
	)
	late = {}
	for tid in reversed(order):
		node = early[tid]
		if node["is_completed"]:
			late[tid] = {"ls": node["es"], "lf": node["ef"]}
			continue
		out_edges = [dep for dep in succs_by_pred[tid] if not early[dep["successor"]]["is_completed"]]
		if not out_edges:
			lf = project_lf
		else:
			lf = None
			for dep in out_edges:
				succ_late = late[dep["successor"]]
				dtype = dep["type"]
				lag = dep["lag"]
				if dtype == "FS":
					cand_lf = _add_days(succ_late["ls"], -lag)
				elif dtype == "SS":
					cand_ls = _add_days(succ_late["ls"], -lag)
					cand_lf = _add_days(cand_ls, node["dur"])
				elif dtype == "FF":
					cand_lf = _add_days(succ_late["lf"], -lag)
				else:
					continue
				if lf is None or _diff_days(cand_lf, lf) > 0:
					lf = cand_lf
			if lf is None or _diff_days(project_lf, lf) > 0:
				lf = project_lf
		ls = _add_days(lf, -node["dur"])
		late[tid] = {"ls": ls, "lf": lf}

	results = []
	for tid in tasks_by_id:
		node = early[tid]
		lnode = late[tid]
		if node["is_completed"]:
			total_float = None
			free_float = None
			critical = False
		else:
			total_float = _diff_days(node["ef"], lnode["lf"])
			out_edges = [dep for dep in succs_by_pred[tid] if not early[dep["successor"]]["is_completed"]]
			if not out_edges:
				free_float = total_float
			else:
				ff_vals = []
				pred_proxy = {
					"start": node["es"],
					"end": node["ef"],
					"type": node["type"],
				}
				for dep in out_edges:
					succ_node = early[dep["successor"]]
					succ_proxy = {
						"start": succ_node["es"],
						"end": succ_node["ef"],
						"type": succ_node["type"],
					}
					edge_es = _earliest_start_for_edge(pred_proxy, succ_proxy, dep, succ_node["dur"])
					if edge_es is not None:
						ff_vals.append(_diff_days(edge_es, succ_node["es"]))
				free_float = min(min(ff_vals), total_float) if ff_vals else total_float
			critical = total_float <= 0

		results.append(
			{
				"task": tid,
				"early_start": _iso(node["es"]),
				"early_finish": _iso(node["ef"]),
				"late_start": _iso(lnode["ls"]),
				"late_finish": _iso(lnode["lf"]),
				"total_float": total_float,
				"free_float": free_float,
				"critical": critical,
			}
		)
	return {"tasks": results, "skipped": skipped}


# --- frappe bindings: load graph, persist flags, recompute, reschedule --------


def _load_graph(project):
	tasks = frappe.get_all(
		"Task",
		filters={"project": project},
		fields=[
			"name",
			"type",
			"exp_start_date",
			"exp_end_date",
			"act_start_date",
			"status",
			"parent_task",
		],
		limit_page_length=0,
	)
	tasks_by_id = {}
	for t in tasks:
		tasks_by_id[t.name] = {
			"name": t.name,
			"type": t.type or "Activity",
			"start": getdate(t.exp_start_date) if t.exp_start_date else None,
			"end": getdate(t.exp_end_date) if t.exp_end_date else None,
			"status": t.status,
			"parent_task": t.parent_task,
			"act_start_date": getdate(t.act_start_date) if getattr(t, "act_start_date", None) else None,
		}
	names = list(tasks_by_id.keys())
	edges = []
	if names:
		rows = frappe.get_all(
			"Task Depends On",
			filters={"parent": ["in", names], "parenttype": "Task"},
			fields=["parent", "task", "dependency_type", "lag_days"],
		)
		for r in rows:
			# Only edges whose predecessor is also in this project's set. ERPNext
			# mirrors the child -> parent tree relation into depends_on; that is
			# not a schedule dependency.
			if r.task in tasks_by_id and tasks_by_id[r.task].get("parent_task") != r.parent:
				edges.append(
					{
						"predecessor": r.task,
						"successor": r.parent,
						"type": r.dependency_type or "FS",
						"lag": r.lag_days or 0,
					}
				)
	return tasks_by_id, edges


def _load_cpm_graph(project):
	tasks = frappe.get_list(
		"Task",
		filters={"project": project},
		fields=[
			"name",
			"type",
			"exp_start_date",
			"exp_end_date",
			"act_start_date",
			"act_end_date",
			"progress",
			"status",
			"is_group",
			"parent_task",
		],
		limit_page_length=0,
	)
	tasks_by_id = {}
	for t in tasks:
		tasks_by_id[t.name] = {
			"name": t.name,
			"type": t.type or "Activity",
			"start": getdate(t.exp_start_date) if t.exp_start_date else None,
			"end": getdate(t.exp_end_date) if t.exp_end_date else None,
			"status": t.status,
			"is_group": bool(t.is_group),
			"progress": flt(t.progress),
			"parent_task": t.parent_task,
			"act_start_date": getdate(t.act_start_date) if t.act_start_date else None,
			"act_end_date": getdate(t.act_end_date) if t.act_end_date else None,
		}
	names = list(tasks_by_id.keys())
	edges = []
	if names:
		rows = frappe.get_list(
			"Task Depends On",
			parent_doctype="Task",
			filters={"parent": ["in", names], "parenttype": "Task"},
			fields=["parent", "task", "dependency_type", "lag_days"],
			limit_page_length=0,
		)
		for r in rows:
			if r.task in tasks_by_id and tasks_by_id[r.task].get("parent_task") != r.parent:
				edges.append(
					{
						"predecessor": r.task,
						"successor": r.parent,
						"type": r.dependency_type or "FS",
						"lag": r.lag_days or 0,
					}
				)
	return tasks_by_id, edges


def _apply_moves(root, moves, root_override=None, native_reschedule=False):
	frappe.flags.in_schedule_cascade = True
	try:
		if root_override:
			if root_override.get("start") is not None:
				root.exp_start_date = root_override["start"]
			if root_override.get("end") is not None:
				root.exp_end_date = root_override["end"]
			root.save()
		for m in moves:
			if root_override and m.get("task") == getattr(root, "name", None):
				continue
			t = frappe.get_doc("Task", m["task"])
			if m.get("is_milestone"):
				# normalize_milestone_task collapses start to end on save.
				t.exp_start_date = m["new_end"]
				t.exp_end_date = m["new_end"]
			else:
				t.exp_start_date = m["new_start"]
				t.exp_end_date = m["new_end"]
			if native_reschedule:
				t.flags.ignore_recursion_check = True
				t.flags.rescheduled = True
				t.save(ignore_permissions=root.flags.ignore_permissions)
			else:
				t.save()
	finally:
		frappe.flags.in_schedule_cascade = False


def _set_conflict(name, conflict, reason):
	cur = frappe.db.get_value("Task", name, ["schedule_conflict", "conflict_reason"], as_dict=True)
	if not cur:
		return
	new_flag = 1 if conflict else 0
	if (cur.schedule_conflict or 0) != new_flag or (cur.conflict_reason or "") != (reason or ""):
		# Direct write — no doc events, so this never re-triggers the on_update hook.
		frappe.db.set_value(
			"Task",
			name,
			{"schedule_conflict": new_flag, "conflict_reason": reason or ""},
			update_modified=False,
		)


def recompute_schedule_conflicts(root_task):
	"""Recompute + persist schedule_conflict/conflict_reason for root_task and its
	downstream subgraph. Cheap when the task has no dependency edges."""
	if not root_task:
		return
	# Fast path: a task in no edge can never conflict — just clear its own flag.
	in_edge = frappe.db.exists("Task Depends On", {"parent": root_task, "parenttype": "Task"})
	is_pred = frappe.db.exists("Task Depends On", {"task": root_task, "parenttype": "Task"})
	if not in_edge and not is_pred:
		_set_conflict(root_task, False, "")
		return
	project = frappe.db.get_value("Task", root_task, "project")
	if not project:
		return
	tasks_by_id, edges = _load_graph(project)
	for tid, (conflict, reason) in compute_conflicts(root_task, tasks_by_id, edges).items():
		_set_conflict(tid, conflict, reason)


def recompute_conflicts_on_update(doc, method=None):
	"""Task on_update hook — re-flag this task's downstream subgraph. Skipped
	during a cascade commit (the cascade re-flags once at the end)."""
	if frappe.flags.get("in_schedule_cascade"):
		return
	recompute_schedule_conflicts(doc.name)


@frappe.whitelist()
def reschedule_downstream(
	task: str,
	new_start: str | None = None,
	new_end: str | None = None,
	dry_run: int = 1,
	asap: bool | int = False,
	slip_days: int | None = None,
):
	"""Preview (dry_run=1) or commit (dry_run=0) a duration-preserving downstream
	cascade after moving `task` to new_start/new_end or slipping it by slip_days.
	With asap=1, eligible tasks may also be pulled back to their earliest
	relationship-bound dates. Returns {"moves": [...]}.

	Commit writes each affected task via doc.save(), so Task write permission is
	enforced per task and the stage-sync / progress hooks still run; conflict
	re-flagging is suppressed per-save and done once at the end.
	"""
	dry_run = int(dry_run)
	if not task:
		frappe.throw(_("task is required"))
	if slip_days is not None and (new_start or new_end):
		frappe.throw(_("slip_days cannot be combined with new_start or new_end."))
	root = frappe.get_doc("Task", task)
	tasks_by_id, edges = _load_graph(root.project)
	if task not in tasks_by_id:
		frappe.throw(_("Task is not part of a project schedule."))

	root_override = None
	slip = None
	if slip_days is not None:
		slip = int(slip_days)
		old_start = getdate(root.exp_start_date) if root.exp_start_date else None
		old_end = getdate(root.exp_end_date) if root.exp_end_date else None
		if old_start is None and old_end is None:
			frappe.throw(_("slip_days requires the root task to have at least one expected date."))
		root_override = {
			"start": _add_days(old_start, slip),
			"end": _add_days(old_end, slip),
		}
	elif new_start or new_end:
		old_start = getdate(root.exp_start_date) if root.exp_start_date else None
		old_end = getdate(root.exp_end_date) if root.exp_end_date else None
		duration = _diff_days(old_start, old_end)
		start = getdate(new_start) if new_start else old_start
		end = getdate(new_end) if new_end else old_end
		if new_start and not new_end and start and old_start and old_end:
			end = _add_days(start, duration)
		elif new_end and not new_start and end and old_start and old_end:
			start = _add_days(end, -duration)
		root_override = {
			"start": start,
			"end": end,
		}

	moves = compute_cascade(task, tasks_by_id, edges, root_override, asap=bool(asap))
	if moves is None:
		frappe.throw(_("Cycle detected in the downstream schedule — cascade aborted."))

	root_changed = bool(
		root_override
		and (
			root_override.get("start") != (getdate(root.exp_start_date) if root.exp_start_date else None)
			or root_override.get("end") != (getdate(root.exp_end_date) if root.exp_end_date else None)
		)
	)
	if root_changed and slip_days is not None:
		root_move = {
			"task": task,
			"old_start": _iso(getdate(root.exp_start_date) if root.exp_start_date else None),
			"old_end": _iso(getdate(root.exp_end_date) if root.exp_end_date else None),
			"new_start": _iso(root_override.get("start")),
			"new_end": _iso(root_override.get("end")),
			"reason": f"slip {slip} days" if slip_days is not None else "root override",
			"is_milestone": tasks_by_id[task].get("type") == "Milestone",
		}
		moves = [root_move, *moves]

	if dry_run:
		return {"moves": moves}

	# A downstream cascade is a *group* action — capture an undo snapshot of the
	# project schedule before mutating, so the whole shift can be reverted in one
	# step. Explicit new_start/new_end-only changes stay single-task changes;
	# slip and ASAP root alignment changes are represented in moves.
	if moves:
		from buildsuite_core.api.schedule_snapshot import capture_snapshot

		capture_snapshot(root.project, "Undo", trigger="reschedule_downstream", root_task=task)

	_apply_moves(root, moves, root_override if root_changed else None)

	recompute_schedule_conflicts(task)
	return {"moves": moves}


@frappe.whitelist()
def get_critical_path(project: str, data_date: str | None = None):
	"""Compute the project's CPM schedule and float on read without persisting."""
	if not project:
		frappe.throw(_("project is required"))
	if not frappe.has_permission("Project", "read", project):
		frappe.throw(_("You are not permitted to view this schedule."), frappe.PermissionError)
	tasks_by_id, edges = _load_cpm_graph(project)
	return compute_cpm(tasks_by_id, edges, data_date or today())
