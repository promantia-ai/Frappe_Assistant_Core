// Decides where a composer submit goes. Pending-card routing wins over
// streaming: a locally-pending card means the user's text is either the
// card's answer (question) or an abandon signal (approval).
export function resolveComposerRoute({ isStreaming, pendingInteraction }) {
	if (pendingInteraction) {
		// Answered already, the answer on its way: the text waits its turn, rather than answer
		// the card again or stop the run that is acting on the answer (#34548)
		if (pendingInteraction.block?.decision) return "queue";
		return pendingInteraction.regime === "question" ? "answer" : "abort-then-send";
	}
	if (isStreaming) return "queue";
	return "send";
}
