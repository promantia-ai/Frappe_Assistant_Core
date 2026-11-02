// Frappe Assistant Core - AI Assistant integration for Frappe Framework
// Copyright (C) 2025 Paul Clinton
//
// This program is free software: you can redistribute it and/or modify it under
// the terms of the GNU Affero General Public License as published by the Free
// Software Foundation, either version 3 of the License, or (at your option) any
// later version. See https://www.gnu.org/licenses/.
//
// The setup plan's form (#34660). A plan is drafted and changed in chat; once approved it is the
// record of what was applied, so the form is read-only, says the run in plain words and links the
// setup record. The server refuses changes to an approved plan either way.

frappe.ui.form.on("FAC Setup Plan", {
	refresh(frm) {
		const frozen = frm.doc.status !== "Draft";
		if (frozen) {
			frm.disable_save();
			frm.set_read_only();
			["items", "gaps"].forEach((table) => {
				const grid = frm.get_field(table).grid;
				grid.cannot_add_rows = true;
				grid.static_rows = true;
				grid.refresh();
			});
		}
		if (frm.doc.status === "Running" && !frm.doc.stop_requested) {
			frm.add_custom_button(__("Stop"), () =>
				frappe.confirm(
					__("Stop the plan before its next item? What it applied so far stays."),
					() => frm.call("stop").then(() => frm.reload_doc())
				)
			);
		}

		// each area by its title, as discovery asks about it, not its key (payment_terms)
		const titles = (frm.doc.__onload && frm.doc.__onload.area_titles) || {};
		frm.fields_dict.items.grid.update_docfield_property("area", "formatter", (value) =>
			frappe.utils.escape_html(titles[value] || value || "")
		);
		frm.fields_dict.items.grid.refresh();

		render_run(frm);
	},
});

function parse(value, fallback) {
	try {
		return JSON.parse(value || "") || fallback;
	} catch (e) {
		return fallback;
	}
}

// The run as a short list (when, what, outcome) instead of the raw Progress and Log JSON
function render_run(frm) {
	const $wrapper = frm.get_field("run_summary").$wrapper;
	const esc = frappe.utils.escape_html;
	const progress = parse(frm.doc.progress, {});
	const log = parse(frm.doc.log, []);
	if (!progress.started_at && !log.length) {
		$wrapper.html(`<p class="text-muted">${__("The plan hasn't run yet.")}</p>`);
		return;
	}
	const when = (at) => (at ? frappe.datetime.str_to_user(String(at).slice(0, 16)) : "");
	const rows = [];
	if (progress.started_at) {
		rows.push([when(progress.started_at), __("Started by {0}", [progress.user || ""]), ""]);
	}
	log.forEach((entry) => rows.push([when(entry.at), entry.message || "", entry.outcome || ""]));
	const color = { Success: "green", Warning: "orange", Failed: "red" };
	$wrapper.html(`
		<table class="table table-bordered table-sm" style="margin-top: 8px">
			<thead><tr>
				<th style="width: 150px">${__("When")}</th><th>${__("What")}</th><th style="width: 110px">${__("Outcome")}</th>
			</tr></thead>
			<tbody>${rows
				.map(
					([at, what, outcome]) => `<tr>
						<td>${esc(at)}</td><td>${esc(what)}</td>
						<td>${
							outcome
								? `<span class="indicator-pill ${color[outcome] || "gray"}">${esc(__(outcome))}</span>`
								: ""
						}</td></tr>`
				)
				.join("")}</tbody>
		</table>
	`);
}
