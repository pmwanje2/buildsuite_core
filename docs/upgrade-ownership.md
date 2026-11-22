# Site-owned permissions and native Timesheets

By default, BuildSuite does not sync its permission matrices from Frappe patches
or migration hooks. Sites manage those permissions separately. Fresh-install
defaults and the existing explicit admin restore actions still work. A site
that deliberately delegates upgrade-time permission changes to BuildSuite can
set the JSON boolean `"buildsuite_sync_permissions_on_migrate": true` in its
site config before migrating. This permits authoritative vendor writes and the
associated expense-owner backfill. It does not replay already-recorded patches.

The BuildSuite workforce Timesheet hooks are disabled by default. ERPNext keeps
its native costing, validation and lifecycle. The JSON boolean
`"buildsuite_enable_labour_timesheets": true` enables BuildSuite's costing,
daily limits and register lifecycle for sites dedicating Timesheet to that
workflow. Do not enable it on sites that also use native project/daywork
Timesheets. Keep it enabled while cancelling or removing previously generated
registers through the worker Timesheets that own them.

Machinery Usage remains editable and non-submittable. Saving a cost-coded log
contributes to BOQ Actual; edits update it and deleting the log removes it.
