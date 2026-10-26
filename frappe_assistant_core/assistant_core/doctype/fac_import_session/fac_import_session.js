// Frappe Assistant Core - AI Assistant integration for Frappe Framework
// Copyright (C) 2025 Paul Clinton
// License: AGPL-3.0

// A System Manager's way out for a session no tool moves on any more (#34538).
frappe.ui.form.on("FAC Import Session", {
	refresh(frm) {
		const ended = ["Completed", "Cancelled", "Undone"].includes(frm.doc.status);
		if (frm.is_new() || ended || !frappe.user.has_role("System Manager")) return;
		frm.add_custom_button(__("Release Session"), () => {
			frappe.confirm(
				__("Cancel this import session, so it stops holding the user's file? Records it imported stay."),
				() =>
					frappe
						.call({
							method: "frappe_assistant_core.assistant_core.doctype.fac_import_session.fac_import_session.release",
							args: { session: frm.doc.name },
						})
						.then(() => frm.reload_doc())
			);
		});
	},
});
