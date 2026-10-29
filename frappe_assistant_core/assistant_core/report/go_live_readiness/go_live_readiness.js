// Frappe Assistant Core - AI Assistant integration for Frappe Framework
// Copyright (C) 2025 Paul Clinton
// License: GNU Affero General Public License v3 or later

frappe.query_reports["Go-Live Readiness"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			description: __("Leave blank to check every company."),
		},
		{
			fieldname: "document",
			label: __("Message as a Document (for the PDF)"),
			fieldtype: "Check",
		},
	],
	onload(report) {
		// The go-live checklist above the filters: these checks are its step 5 (#34419)
		if (!frappe_assistant_core.go_live) return;
		const $slot = $('<div class="fac-go-live-report-slot" style="padding: 0 var(--padding-md, 15px)"></div>');
		$slot.insertBefore(report.page.page_form);
		frappe_assistant_core.go_live.load_checklist($slot);
	},
};
