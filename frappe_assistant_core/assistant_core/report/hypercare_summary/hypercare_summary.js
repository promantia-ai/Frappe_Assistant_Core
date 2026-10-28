// Frappe Assistant Core - AI Assistant integration for Frappe Framework
// Copyright (C) 2025 Paul Clinton
// License: GNU Affero General Public License v3 or later

frappe.query_reports["Hypercare Summary"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "stuck_after_days",
			label: __("Approval Stuck After (Days)"),
			fieldtype: "Int",
			description: __("Leave blank to use the active hypercare's setting."),
		},
		{
			fieldname: "document",
			label: __("Message as a Document (for the PDF)"),
			fieldtype: "Check",
		},
	],
};
