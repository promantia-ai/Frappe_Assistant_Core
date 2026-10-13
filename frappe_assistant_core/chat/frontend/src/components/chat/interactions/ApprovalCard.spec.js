import { mount } from "@vue/test-utils";
import { describe, it, expect } from "vitest";

import ApprovalCard from "@/components/chat/interactions/ApprovalCard.vue";

function card(input, toolName = "create_document") {
	return mount(ApprovalCard, { props: { block: { id: "b1", tool_name: toolName, input, interrupts: [] } } });
}

const masters = (count) =>
	Array.from({ length: count }, (_, i) => ({
		doctype: "Customer Group",
		data: { customer_group_name: `Group ${i + 1}`, parent_customer_group: "All Customer Groups" },
	}));

describe("ApprovalCard with several records", () => {
	it("lists every record being approved, not 'N items'", () => {
		const wrapper = card({ documents: masters(3), import_session: "IMP-1" });

		const items = wrapper.findAll(".records-list li").map((li) => li.text());
		expect(items).toEqual([
			"Customer Group · Group 1 (under All Customer Groups)",
			"Customer Group · Group 2 (under All Customer Groups)",
			"Customer Group · Group 3 (under All Customer Groups)",
		]);
		expect(wrapper.find(".records-heading").text()).toBe("3 records");
		expect(wrapper.text()).not.toContain("3 items");
		expect(wrapper.find(".approval-subtitle").text()).toBe(
			"This will write 3 new records to your ERP, all or none",
		);
	});

	it("shows the first ten and the rest on request", async () => {
		const wrapper = card({ documents: masters(12) });

		expect(wrapper.findAll(".records-list li")).toHaveLength(10);
		await wrapper.find(".approval-records .field-toggle").trigger("click");
		expect(wrapper.findAll(".records-list li")).toHaveLength(12);
	});

	it("says how many records an update list changes", () => {
		const wrapper = card(
			{
				documents: [
					{ doctype: "Customer", name: "C-1", data: { territory: "Karnataka" } },
					{ doctype: "Customer", name: "C-2", data: { territory: "Karnataka" } },
				],
			},
			"update_document",
		);

		expect(wrapper.find(".approval-subtitle").text()).toBe(
			"This will modify 2 existing records in your ERP, all or none",
		);
	});

	it("leaves a single-record card as it was", () => {
		const wrapper = card({ doctype: "ToDo", data: { description: "Call the bank" } });

		expect(wrapper.find(".approval-records").exists()).toBe(false);
		expect(wrapper.find(".approval-subtitle").text()).toBe("This will write a new record to your ERP");
		expect(wrapper.find(".approval-table").text()).toContain("Call the bank");
	});
});
