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
};
