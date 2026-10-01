import { describe, it, expect, vi, beforeEach } from "vitest";
import { friendlyError } from "@/api/_core";

vi.mock("@/api/client", () => ({
	api: { chat: { uploadFile: vi.fn() } },
}));

const showError = vi.fn();
vi.mock("@/composables/useToast", () => ({
	useToast: () => ({ showError }),
}));

import { api } from "@/api/client";
import { useMessageFileUpload } from "./useMessageFileUpload";

const REJECTION = "File content does not match its extension: .xlsx";

// What Frappe returns for frappe.throw(..., ValidationError): HTTP 417 with the
// message triple-encoded in _server_messages.
const rejectionBody = JSON.stringify({
	exception: `frappe.exceptions.ValidationError: ${REJECTION}`,
	exc_type: "ValidationError",
	_server_messages: JSON.stringify([
		JSON.stringify({ message: REJECTION, title: "Message", indicator: "red", raise_exception: 1 }),
	]),
});

describe("useMessageFileUpload", () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it("shows the server's rejection and drops that file's chip", async () => {
		const bad = new File(["MZ"], "payroll.xlsx");
		api.chat.uploadFile.mockRejectedValue(friendlyError({ status: 417 }, rejectionBody));
		const onFailed = vi.fn();

		const { uploadedFiles, handleFileUpload } = useMessageFileUpload();
		await handleFileUpload([bad], onFailed);

		expect(showError).toHaveBeenCalledWith(`payroll.xlsx: ${REJECTION}`);
		expect(onFailed.mock.calls[0][0]).toBe(bad);
		expect(uploadedFiles.value).toEqual([]);
	});

	it("keeps uploading the rest after one file fails", async () => {
		const bad = new File(["MZ"], "payroll.xlsx");
		const good = new File(["a,b"], "customers.csv");
		const uploaded = { file_name: "customers.csv", file_url: "/private/files/customers.csv" };
		api.chat.uploadFile
			.mockRejectedValueOnce(friendlyError({ status: 417 }, rejectionBody))
			.mockResolvedValueOnce(uploaded);
		const onFailed = vi.fn();

		const { uploadedFiles, handleFileUpload } = useMessageFileUpload();
		await handleFileUpload([bad, good], onFailed);

		expect(onFailed).toHaveBeenCalledTimes(1);
		expect(onFailed.mock.calls[0][0]).toBe(bad);
		expect(uploadedFiles.value).toEqual([uploaded]);
	});
});
