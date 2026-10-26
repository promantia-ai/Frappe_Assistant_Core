import { describe, it, expect } from "vitest";
import { resolveComposerRoute } from "./composerRouting";

describe("resolveComposerRoute", () => {
	it("routes to answer when a question card is pending", () => {
		expect(
			resolveComposerRoute({
				isStreaming: false,
				pendingInteraction: { regime: "question" },
			})
		).toBe("answer");
	});

	it("routes to abort-then-send when an approval card is pending", () => {
		expect(
			resolveComposerRoute({
				isStreaming: false,
				pendingInteraction: { regime: "approval" },
			})
		).toBe("abort-then-send");
	});

	it("pending interaction wins over isStreaming", () => {
		// A resume stream can be racing while a card is still marked pending
		// locally; card routing must win so the user's text isn't misdelivered.
		expect(
			resolveComposerRoute({
				isStreaming: true,
				pendingInteraction: { regime: "question" },
			})
		).toBe("answer");
	});

	it("queues the text once the pending card's answer is given and on its way", () => {
		// #34548: text typed after a click answered the card again and stopped FACO mid-run
		for (const regime of ["question", "approval"]) {
			expect(
				resolveComposerRoute({
					isStreaming: false,
					pendingInteraction: { regime, block: { decision: { resolution: "answered" } } },
				})
			).toBe("queue");
		}
	});

	it("routes to queue while streaming with no pending card", () => {
		expect(resolveComposerRoute({ isStreaming: true, pendingInteraction: null })).toBe("queue");
	});

	it("routes to send when idle", () => {
		expect(resolveComposerRoute({ isStreaming: false, pendingInteraction: null })).toBe("send");
	});
});
