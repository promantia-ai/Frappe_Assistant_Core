// Frappe Assistant Core - go-live banner and checklist (#34419)
// Copyright (C) 2025 Paul Clinton
//
// This program is free software: you can redistribute it and/or modify it under
// the terms of the GNU Affero General Public License as published by the Free
// Software Foundation, either version 3 of the License, or (at your option) any
// later version. See https://www.gnu.org/licenses/.
//
// Loads on every Desk page. The server endpoint
// frappe_assistant_core.onboarding.api.get_entry_state decides everything (who
// sees it, the stage, the steps, the wording); this file only renders it. The
// banner asks once per full page load. The checklist is the same data, shown on
// the Go-Live Readiness report and FAC Admin. Any error hides it silently.

frappe.provide("frappe_assistant_core.go_live");

(function () {
	const ns = frappe_assistant_core.go_live;
	const API = "frappe_assistant_core.onboarding.api.";
	const STATE_LABELS = {
		done: __("Done"),
		needs_attention: __("Needs attention"),
		next: __("Next step"),
		not_started: __("Not started"),
		skipped: __("Skipped"),
	};

	ns.add_styles = function () {
		if (document.getElementById("fac-go-live-styles")) return;
		const css = `
			.fac-go-live-banner { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
				margin: 10px 20px; padding: 10px 14px; border-radius: var(--border-radius-md, 8px);
				background: var(--green-50, #ecfdf3); border: 1px solid var(--green-200, #b5ecc9);
				color: var(--text-color); }
			.fac-go-live-banner .fac-go-live-icon { display: inline-flex; align-items: center; justify-content: center;
				width: 26px; height: 26px; border-radius: 6px; background: var(--green-600, #16794c); color: #fff; }
			.fac-go-live-banner .fac-go-live-text { flex: 1; min-width: 200px; }
			.fac-go-live-banner .fac-go-live-note { color: var(--text-muted); }
			.fac-go-live-checklist { margin: 0 0 16px; padding: 16px; border: 1px solid var(--border-color);
				border-radius: var(--border-radius-md, 8px); background: var(--card-bg, var(--fg-color)); }
			.fac-go-live-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
			.fac-go-live-head h4 { flex: 1; margin: 0; font-size: var(--text-lg, 16px); }
			.fac-go-live-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
			.fac-go-live-step { padding: 10px; border: 1px solid var(--border-color); border-radius: 8px; min-height: 66px;
				display: flex; flex-direction: column; }
			.fac-go-live-step .step-desc { font-size: var(--text-sm, 12px); color: var(--text-color); margin-top: 4px; }
			.fac-go-live-step.state-next { border-color: var(--green-500, #2bb673); background: var(--green-50, #ecfdf3); }
			.fac-go-live-step.state-needs_attention { border-color: var(--orange-400, #f8a83a); }
			.fac-go-live-step .step-label { font-weight: 600; }
			.fac-go-live-step .step-meta { font-size: var(--text-sm, 12px); color: var(--text-muted); margin-top: 4px; }
			.fac-go-live-step .step-actions { margin-top: auto; padding-top: 8px; display: flex; gap: 8px; flex-wrap: wrap;
				align-items: center; }
			.fac-go-live-step .step-actions a { font-size: var(--text-sm, 12px); cursor: pointer; }
			.fac-go-live-foot { margin-top: 10px; font-size: var(--text-sm, 12px); color: var(--text-muted); }
		`;
		$("<style id='fac-go-live-styles'>").text(css).appendTo("head");
	};

	ns.call = function (method, args, type) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: API + method,
				type: type || "POST",
				args: args || {},
				callback: (r) => resolve(r && r.message),
				error: reject,
			});
		});
	};

	ns.form_link = function (record) {
		if (!record) return "";
		return frappe.utils.get_form_link(record.doctype, record.name);
	};

	// --- banner -------------------------------------------------------------------

	ns.check_and_show_banner = function () {
		if (!window.frappe || !frappe.call || !frappe.session || frappe.session.user === "Guest") return;
		ns.call("get_entry_state", { view: "banner" }, "GET")
			.then((state) => {
				if (state && state.show) ns.render_banner(state);
			})
			.catch(() => {});
	};

	ns.render_banner = function (state) {
		ns.add_styles();
		$(".fac-go-live-banner").remove();
		const $target = $(".main-section, .layout-main-section, .page-body").first();
		if (!$target.length) return;

		const $banner = $(`
			<div class="fac-go-live-banner" role="region" aria-label="${__("Go-live")}">
				<span class="fac-go-live-icon" aria-hidden="true">&rarr;</span>
				<div class="fac-go-live-text">
					<strong></strong>
					<span class="fac-go-live-note">${__("Only System Managers see this.")}</span>
				</div>
			</div>
		`);
		$banner.find("strong").text(state.text);
		if (state.primary) {
			$(`<button type="button" class="btn btn-primary btn-xs"></button>`)
				.text(state.primary)
				.on("click", () => ns.primary(state))
				.appendTo($banner);
		}
		if (state.buttons.includes("not_now")) {
			$(`<button type="button" class="btn btn-default btn-xs">${__("Not now")}</button>`)
				.on("click", () =>
					ns.call("snooze", { company: state.company }).finally(() => $banner.remove())
				)
				.appendTo($banner);
		}
		if (state.buttons.includes("already_live")) {
			$(`<button type="button" class="btn btn-default btn-xs">${__("Already live")}</button>`)
				.on("click", () => ns.confirm_live(state, () => $banner.remove()))
				.appendTo($banner);
		}
		$target.prepend($banner);
	};

	ns.primary = function (state) {
		if (state.variant === "enable_tools") {
			ns.call("enable_tools")
				.then((next) => {
					$(".fac-go-live-banner").remove();
					if (next && next.show) ns.render_banner(next);
				})
				.catch(() => {});
		} else if (state.variant === "hypercare") {
			frappe.set_route("query-report", "Hypercare Summary");
		} else {
			ns.open_continue(state);
		}
	};

	ns.confirm_live = function (state, done) {
		frappe.confirm(
			__("Mark {0} as already live? FAC stops offering to take it live, for everyone. You can undo this on FAC Admin.", [
				frappe.utils.escape_html(state.company),
			]),
			() => ns.call("mark_live", { company: state.company }).then(done).catch(() => {})
		);
	};

	// --- continue -----------------------------------------------------------------

	ns.open_continue = function (state) {
		const go = state.continue || {};
		if (go.mode === "fac_chat") {
			window.open("/copilot/chat?prompt=" + encodeURIComponent(go.prompt), "_blank", "noopener");
			return;
		}
		const fields = [
			{
				fieldtype: "HTML",
				options: `<p>${__(
					"Paste this into Claude, ChatGPT or another AI assistant connected to this site:"
				)}</p>`,
			},
			{ fieldname: "prompt", fieldtype: "Small Text", label: __("Prompt"), read_only: 1, default: go.prompt },
			{ fieldname: "endpoint", fieldtype: "Data", label: __("MCP endpoint"), read_only: 1, default: state.mcp_endpoint },
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Clients that list prompts also offer the {0} prompt.",
					["<code>go-live</code>"]
				)}</p>`,
			},
		];
		if (go.chat_on) {
			// FAC Chat is on but can't chat yet (not connected, waitlisted, no access): say why
			fields.unshift({
				fieldtype: "HTML",
				options: `<p class="small">${__("FAC Chat can't take this yet: {0}", [
					frappe.utils.escape_html(go.chat_reason || ""),
				])} <a href="/copilot" target="_blank" rel="noopener">${__("Open FAC Chat")}</a></p>`,
			});
		} else if (state.can_enable_chat) {
			fields.push({
				fieldtype: "HTML",
				options: `<p class="small"><a href="/app/fac-admin">${__("Enable FAC Chat")}</a> ${__(
					"to continue here instead."
				)}</p>`,
			});
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Continue in your AI assistant"),
			fields: fields,
			primary_action_label: __("Copy prompt"),
			primary_action() {
				frappe.utils.copy_to_clipboard(go.prompt);
			},
		});
		dialog.show();
	};

	// --- checklist ----------------------------------------------------------------

	ns.load_checklist = function ($parent, company) {
		return ns
			.call("get_entry_state", { view: "checklist", company: company || null }, "GET")
			.then((state) => {
				$parent.empty();
				if (state && state.show) ns.render_checklist($parent, state);
			})
			.catch(() => $parent.empty());
	};

	ns.render_checklist = function ($parent, state) {
		ns.add_styles();
		const reload = (next) => {
			$parent.empty();
			if (next && next.show) ns.render_checklist($parent, next);
		};
		const done = state.steps.filter((s) => s.state === "done" || s.state === "skipped").length;
		const $box = $(`
			<div class="fac-go-live-checklist">
				<div class="fac-go-live-head"><h4></h4></div>
				<div class="fac-go-live-steps"></div>
				<div class="fac-go-live-foot"></div>
			</div>
		`);
		$box.find("h4").text(__("Take {0} live · {1} of 6", [state.company, done]));
		const $head = $box.find(".fac-go-live-head");

		if (state.companies && state.companies.length > 1) {
			const $select = $(`<select class="form-control input-xs" style="width:auto"></select>`);
			state.companies.forEach((c) => $("<option>").val(c).text(c).appendTo($select));
			$select.val(state.company).on("change", () => ns.load_checklist($parent, $select.val()));
			$head.prepend($select);
		}
		if (state.variant === "enable_tools" && state.can_enable_tools) {
			$(`<button class="btn btn-primary btn-xs">${__("Turn on onboarding tools")}</button>`)
				.on("click", () => ns.call("enable_tools").then(() => ns.load_checklist($parent, state.company)))
				.appendTo($head);
		} else if (state.continue && state.next_step) {
			$(`<button class="btn btn-primary btn-xs"></button>`)
				.text(state.continue.mode === "fac_chat" ? __("Continue in FAC Chat") : __("Continue in your AI assistant"))
				.on("click", () => ns.open_continue(state))
				.appendTo($head);
		}

		const $steps = $box.find(".fac-go-live-steps");
		state.steps.forEach((step) => {
			const icon = { done: "✓", skipped: "–", needs_attention: "!", next: "●" }[step.state] || "○";
			const $step = $(`
				<div class="fac-go-live-step state-${step.state}">
					<div class="step-label"></div>
					<div class="step-desc"></div>
					<div class="step-meta"></div>
					<div class="step-actions"></div>
				</div>
			`);
			$step.find(".step-label").text(`${icon} ${step.label}`);
			$step.find(".step-desc").text(step.description || "");
			$step.find(".step-meta").text(STATE_LABELS[step.state] || step.state);
			const $actions = $step.find(".step-actions");
			// Any step not done can be started on its own: they don't have to be done in order.
			// Not while the onboarding tools are off: the AI would have nothing to do it with.
			if (step.prompt && state.continue && state.variant !== "enable_tools") {
				$(`<button type="button" class="btn btn-default btn-xs">${__("Start")}</button>`)
					.on("click", () =>
						ns.open_continue({
							...state,
							continue: { ...state.continue, prompt: step.prompt },
						})
					)
					.appendTo($actions);
			}
			[step.record, step.setup_record].forEach((record) => {
				if (!record) return;
				$("<a>").attr("href", ns.form_link(record)).text(record.name + " ↗").appendTo($actions);
			});
			if (step.state === "skipped") {
				if (step.skipped && step.skipped.reason) {
					$step.find(".step-meta").text(`${STATE_LABELS.skipped}: ${step.skipped.reason}`);
				}
				$(`<a>${__("Undo")}</a>`)
					.on("click", () =>
						ns.call("unskip_step", { company: state.company, step: step.key }).then(reload)
					)
					.appendTo($actions);
			} else if (step.state !== "done" && step.key !== "discovery") {
				$(`<a>${__("Skip")}</a>`)
					.on("click", () => ns.skip(state, step, reload))
					.appendTo($actions);
			}
			$steps.append($step);
		});

		const $foot = $box.find(".fac-go-live-foot");
		let note = __(
			"Progress is read from this site's records (setup plan, chart, migration plan, test run, hypercare)."
		);
		if ($parent.hasClass("fac-go-live-report-slot")) {
			note += " " + __("The readiness checks below are step 5.");
		}
		$foot.text(note);
		if (state.marked_live) {
			const $live = $("<div>").appendTo($foot);
			$live.text(
				__("Marked live by {0} on {1}. ", [
					state.marked_live.by,
					frappe.datetime.str_to_user(state.marked_live.on),
				])
			);
			$(`<a>${__("Undo")}</a>`)
				.on("click", () => ns.call("unmark_live", { company: state.company }).then(reload))
				.appendTo($live);
		}
		$parent.append($box);
	};

	ns.skip = function (state, step, reload) {
		frappe.prompt(
			[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason (optional)") }],
			(values) =>
				ns
					.call("skip_step", { company: state.company, step: step.key, reason: values.reason })
					.then(reload),
			__("Skip {0}", [step.label]),
			__("Skip")
		);
	};

	$(document).ready(function () {
		// Same moment as the FAC Chat banner: the Desk layout is in place by then
		setTimeout(ns.check_and_show_banner, 500);
	});
})();
