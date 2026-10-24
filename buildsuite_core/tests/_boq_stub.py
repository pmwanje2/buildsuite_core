# Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
# For license information, please see license.txt
"""Fake Frappe stub for unit-testing BOQ work package mass assignment without a bench."""

from __future__ import annotations

import json
import re
import sys
import types
import unittest
from types import SimpleNamespace


def _cint(value, default=0):
	try:
		return int(float(value))
	except Exception:
		return default


def _throw(msg, exc=ValueError):
	if isinstance(exc, type) and issubclass(exc, BaseException):
		raise exc(msg)
	raise exc


def _strip_html_tags(value):
	return re.sub(r"<[^>]+>", "", str(value or ""))


def ensure_frappe_stub():
	try:
		import frappe
	except ModuleNotFoundError:
		frappe = types.ModuleType("frappe")
		frappe.__path__ = []
		frappe._ = lambda value: value
		frappe.DoesNotExistError = LookupError
		frappe.PermissionError = PermissionError
		frappe.ValidationError = ValueError
		frappe.throw = _throw

		def _whitelist():
			def _mark(function):
				function._whitelisted = True
				return function

			return _mark

		frappe.whitelist = _whitelist
		frappe.tests = types.ModuleType("frappe.tests")
		frappe.tests.UnitTestCase = unittest.TestCase
		frappe_utils = types.ModuleType("frappe.utils")
		frappe_utils.cint = _cint
		frappe_utils.flt = lambda value: float(value or 0)
		frappe_utils.today = lambda: "2026-10-24"
		frappe_utils.strip_html_tags = _strip_html_tags
		sys.modules["frappe"] = frappe
		sys.modules["frappe.tests"] = frappe.tests
		sys.modules["frappe.utils"] = frappe_utils
	else:
		frappe_utils = sys.modules.get("frappe.utils")
		if frappe_utils is None:
			frappe_utils = types.ModuleType("frappe.utils")
			sys.modules["frappe.utils"] = frappe_utils
		frappe.utils = frappe_utils
		if not hasattr(frappe_utils, "cint"):
			frappe_utils.cint = _cint
		if not hasattr(frappe_utils, "strip_html_tags"):
			frappe_utils.strip_html_tags = _strip_html_tags
		if not hasattr(frappe, "PermissionError"):
			frappe.PermissionError = PermissionError
		if not hasattr(frappe, "ValidationError"):
			frappe.ValidationError = ValueError
		if not hasattr(frappe, "throw"):
			frappe.throw = _throw
	return frappe


class AssignmentDoc:
	def __init__(self, _origin=None, _frappe=None, save_error=None, **values):
		self._field_names = tuple(values.keys())
		self.__dict__.update(values)
		self._origin = _origin
		self._frappe = _frappe
		self.save_error = save_error
		self.save_calls = []
		self.comments = []

	def clone(self) -> AssignmentDoc:
		values = {name: getattr(self, name) for name in self._field_names}
		return AssignmentDoc(_origin=self, _frappe=self._frappe, save_error=self.save_error, **values)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, **kwargs):
		target = self._origin or self
		target.save_calls.append(kwargs)
		target.work_package = getattr(self, "work_package", None)
		if target.save_error is not None:
			target._frappe.local.message_log.append(str(target.save_error))
			raise target.save_error

	def add_comment(self, comment_type, content):
		target = self._origin or self
		target.comments.append(content)


class AssignmentFlags(dict):
	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


class AssignmentDb:
	def __init__(self, items: dict[str, AssignmentDoc]):
		self._items = items
		self._snapshots: dict[str, dict[str, str | None]] = {}
		self.savepoints: list[str] = []
		self.rollbacks: list[str | None] = []

	def savepoint(self, name: str):
		self.savepoints.append(name)
		self._snapshots[name] = {key: item.work_package for key, item in self._items.items()}

	def rollback(self, *, save_point: str | None = None):
		self.rollbacks.append(save_point)
		if save_point and save_point in self._snapshots:
			for key, wp_name in self._snapshots[save_point].items():
				self._items[key].work_package = wp_name


class AssignmentFrappe:
	def __init__(self, boq, items, work_packages, denied=(), unreadable=()):
		frappe = ensure_frappe_stub()
		self.boq = boq
		self.items = {item.name: item for item in items}
		for item in self.items.values():
			item._frappe = self
		self.work_packages = {work_package.name: work_package for work_package in work_packages}
		self.denied = set(denied)
		self.unreadable = set(unreadable)
		self.permission_calls = []
		self.get_doc_calls = []
		self.get_list_calls = []
		self.get_all_calls = []
		self.error_logs = []
		self.flags = AssignmentFlags()
		self.local = SimpleNamespace(message_log=[])
		self.db = AssignmentDb(self.items)
		self.utils = frappe.utils
		self.DoesNotExistError = getattr(frappe, "DoesNotExistError", LookupError)
		self.PermissionError = getattr(frappe, "PermissionError", PermissionError)
		self.ValidationError = getattr(frappe, "ValidationError", ValueError)
		self.throw = getattr(frappe, "throw", _throw)

	def get_doc(self, doctype, name):
		self.get_doc_calls.append((doctype, name))
		if doctype == "BOQ":
			return self.boq
		if doctype == "BOQ Item":
			if name not in self.items:
				raise self.DoesNotExistError(name)
			return self.items[name].clone()
		if doctype == "Work Package":
			if name not in self.work_packages:
				raise self.DoesNotExistError(name)
			return self.work_packages[name].clone()
		raise AssertionError((doctype, name))

	def get_list(self, doctype, filters=None, fields=None, **kwargs):
		self.get_list_calls.append((doctype, filters, fields, kwargs))
		if doctype != "BOQ Item":
			raise AssertionError((doctype, filters, fields, kwargs))
		return [
			SimpleNamespace(
				name=item.name,
				work_package=item.work_package,
				planned_amount=item.planned_amount,
			)
			for item in self.items.values()
			if item.boq == (filters or {}).get("boq") and item.name not in self.unreadable
		]

	def get_all(self, doctype, filters=None, fields=None, **kwargs):
		self.get_all_calls.append((doctype, filters, fields, kwargs))
		if doctype != "BOQ Item":
			raise AssertionError((doctype, filters, fields, kwargs))
		return [
			SimpleNamespace(
				name=item.name,
				work_package=item.work_package,
				planned_amount=item.planned_amount,
			)
			for item in self.items.values()
			if item.boq == (filters or {}).get("boq")
		]

	def has_permission(self, doctype, ptype, doc=None, **kwargs):
		self.permission_calls.append((doctype, ptype, doc, kwargs))
		doc_name = getattr(doc, "name", doc)
		return doc_name not in self.denied and (doctype, doc_name) not in self.denied

	def parse_json(self, value):
		return json.loads(value)

	def get_traceback(self):
		return "traceback"

	def log_error(self, message=None, title=None):
		self.error_logs.append({"title": title, "message": message})
