import assert from "node:assert/strict";
import test from "node:test";

import { computeCascade } from "./useScheduleEngine.js";

test("ordinary cascade moves declare is_milestone false", () => {
	const tasks = {
		A: { type: "Activity", start: "2026-01-01", end: "2026-01-10" },
		B: { type: "Activity", start: "2026-01-11", end: "2026-01-21" },
	};
	const moves = computeCascade(
		"A",
		tasks,
		[{ predecessor: "A", successor: "B", type: "FS", lag: 0 }],
		{ start: "2026-01-11", end: "2026-01-20" }
	);

	assert.deepEqual(moves, [
		{
			task: "B",
			old_start: "2026-01-11",
			old_end: "2026-01-21",
			new_start: "2026-01-20",
			new_end: "2026-01-30",
			reason: "FS, lag 0 from A",
			is_milestone: false,
		},
	]);
});
