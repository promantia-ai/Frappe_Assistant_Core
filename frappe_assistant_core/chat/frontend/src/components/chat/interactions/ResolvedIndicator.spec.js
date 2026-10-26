import { mount } from "@vue/test-utils";
import { describe, it, expect } from "vitest";

import ResolvedIndicator from "@/components/chat/interactions/ResolvedIndicator.vue";

const question = { status: "answered", options: ["Apply all suggested answers", "Let me change some"] };

describe("ResolvedIndicator on an answered question", () => {
	// #34546: an answer typed in the composer showed only as a small italic line on the card
	it("shows an answer the user typed in full, as theirs", () => {
		const typed = "Map Retial to Retail, and leave the closed shops out.";
		const w = mount(ResolvedIndicator, {
			props: { block: { ...question, userResponse: typed }, interactionType: "single_select" },
		});

		expect(w.find(".resolved-answer").text()).toBe(typed);
		expect(w.text()).toContain("You answered");
	});

	it("names a chosen option on its line, as before", () => {
		const w = mount(ResolvedIndicator, {
			props: { block: { ...question, userResponse: question.options[0] }, interactionType: "single_select" },
		});

		expect(w.find(".resolved-answer").exists()).toBe(false);
		expect(w.text()).toContain("Answered: Apply all suggested answers");
	});
});
