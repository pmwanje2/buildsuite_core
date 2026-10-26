// Copyright (c) 2026, Infraholic Innovations Pvt. Ltd and contributors
// For license information, please see license.txt
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

function form() {
	const handlers = {};
	const calls = [];
	const banners = [];
	const frappe = {
		ui: {
			form: { on(doctype, events) { Object.assign(handlers[doctype] ||= {}, events); } },
			Dialog: function () { assert.fail("PO submission must not propose estimating-rate updates"); },
		},
		call(options) { calls.push(options.method); options.callback?.({ message: 5 }); },
		utils: { escape_html: String },
		validated: true,
	};
	runInNewContext(
		readFileSync(join(__dirname, "../public/js/purchase_order.js"), "utf8"),
		{ frappe, flt: Number, format_currency: String, __: String },
	);
	const frm = {
		doc: {
			docstatus: 0,
			items: [{ custom_rate_master: "RM-1", custom_rate_master_rate: 100, rate: 150 }],
		},
		get_field: () => ({ $wrapper: { html: value => banners.push(value), empty() {} } }),
		set_query() {},
	};
	return { handlers, calls, banners, frappe, frm };
}

test("loading a PO does not request the retired rate-prompt threshold", () => {
	const state = form();
	state.handlers["Purchase Order"].onload(state.frm);
	assert.deepEqual(state.calls, []);
});

test("refreshing an expensive PO does not advertise estimating-rate changes", () => {
	const state = form();
	state.handlers["Purchase Order"].refresh(state.frm);
	assert.deepEqual(state.banners, []);
});

test("submitting an expensive PO does not block submission or open a rate dialog", () => {
	const state = form();
	state.handlers["Purchase Order"].before_submit?.(state.frm);
	assert.equal(state.frappe.validated, true);
	assert.deepEqual(state.calls, []);
});
