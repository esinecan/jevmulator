/**
 * sys1 judge extension for pi.
 *
 * - Registers `submit_verdict`, which forwards the agent's arguments to the daemon's form
 *   unchanged. Every rule about a verdict lives in the daemon, so this file never judges.
 * - Reports the tools and the model pi actually offered to the daemon (`hello`), before
 *   the first model call. The daemon ends the run when they differ from the profile.
 * - Nudges an agent that stops without a verdict, at most SYS1_MAX_NUDGES times.
 * - Confines `write` and `edit` to the work directory, confines reads to the profile's
 *   read roots when it sets them, and bounds `bash`.
 * - In a shell profile, replaces `bash` with one whose working directory is the work
 *   directory and whose environment has no keys or tokens. This is best effort: pi has no
 *   operating-system sandbox on Windows.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createBashTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs";
import * as path from "node:path";

const SUBMIT_URL = process.env.SYS1_SUBMIT_URL ?? "";
const HELLO_URL = process.env.SYS1_HELLO_URL ?? "";
const TOKEN = process.env.SYS1_RUN_TOKEN ?? "";
const MAX_NUDGES = Number(process.env.SYS1_MAX_NUDGES ?? "2");
const WORK_DIR = path.resolve(process.env.SYS1_WORK_DIR ?? process.cwd());
const RUN_DIR = path.resolve(process.env.SYS1_RUN_DIR ?? path.dirname(WORK_DIR));
const REPLACE_BASH = process.env.SYS1_REPLACE_BASH === "1";
const BASH_TIMEOUT_SECONDS = 120;
const SECRET_NAME = /(_API_KEY|_TOKEN|_SECRET)$/i;

const READ_ROOTS: string[] = (() => {
	try {
		const value = JSON.parse(process.env.SYS1_READ_ROOTS ?? "[]");
		return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
	} catch {
		return [];
	}
})();

async function post(url: string, body: unknown): Promise<{ status: number; data: any }> {
	const response = await fetch(url, {
		method: "POST",
		headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
		body: JSON.stringify(body),
	});
	const text = await response.text();
	let data: any;
	try {
		data = JSON.parse(text);
	} catch {
		data = { message: text };
	}
	return { status: response.status, data };
}

function canonical(target: string): string {
	let resolved = path.resolve(WORK_DIR, target.replace(/^@/, ""));
	try {
		resolved = fs.realpathSync.native(resolved);
	} catch {
		// A file that does not exist yet keeps its lexical path.
	}
	return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function inside(target: string, root: string): boolean {
	const relative = path.relative(canonical(root), canonical(target));
	return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function scrubbed(env: Record<string, string | undefined> | undefined): Record<string, string> {
	const clean: Record<string, string> = {};
	for (const [name, value] of Object.entries(env ?? {})) {
		if (value === undefined) continue;
		if (SECRET_NAME.test(name) || name.startsWith("SYS1_")) continue;
		clean[name] = value;
	}
	return clean;
}

export default function (pi: ExtensionAPI) {
	let accepted = false;
	let closed = false;
	let helloSent = false;
	let nudges = 0;

	pi.on("before_agent_start", async (event: any, ctx: any) => {
		if (helloSent) return;
		helloSent = true;
		const tools = pi.getActiveTools();
		const model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "unknown";
		try {
			const { data } = await post(HELLO_URL, {
				tools,
				model,
				selected_tools: event?.systemPromptOptions?.selectedTools ?? null,
			});
			if (data && data.ok === false) {
				closed = true;
				ctx.abort();
			}
		} catch {
			// The daemon's hello deadline decides what a lost hello means.
		}
	});

	pi.registerTool({
		name: "submit_verdict",
		label: "Submit verdict",
		description:
			"Submit your verdict to the daemon's form: one answer object per question label, " +
			"a rationale, and optional evidence. The form either accepts it or lists the " +
			"problems to fix.",
		promptSnippet: "Submit the final verdict: an answer per question label, a rationale, evidence",
		promptGuidelines: [
			"Call submit_verdict when your research is done. After submit_verdict accepts, stop.",
		],
		parameters: Type.Object({
			answers: Type.Object(
				{},
				{
					additionalProperties: true,
					description: "One answer object per question label, in the exact shape the brief gives",
				},
			),
			rationale: Type.String({
				description: "What you checked, and why the distributions look the way they do",
			}),
			evidence: Type.Optional(
				Type.Array(Type.String(), {
					description: "Short references: path:line, or a command and its result",
				}),
			),
		}),
		async execute(_toolCallId: string, params: any) {
			const { status, data } = await post(SUBMIT_URL, params);
			if (status === 200 && data?.accepted === true) {
				accepted = true;
				return {
					content: [{ type: "text", text: "Accepted. Your verdict is recorded. Stop now." }],
					details: { accepted: true },
					terminate: true,
				};
			}
			if (status === 200 && data?.accepted === false) {
				const left = Number(data.attempts_left ?? 0);
				const lines = (data.problems ?? []).map(
					(item: any) => `- ${item.path || "(submission)"}: ${item.problem}`,
				);
				if (left <= 0) {
					closed = true;
					return {
						content: [
							{ type: "text", text: "Rejected, and no attempts are left. Stop now.\n" + lines.join("\n") },
						],
						details: { accepted: false, closed: true },
						terminate: true,
					};
				}
				throw new Error(
					`The form rejected the submission. Fix exactly these problems and call ` +
						`submit_verdict again (${left} attempts left):\n` +
						lines.join("\n"),
				);
			}
			closed = true;
			const message = data?.detail?.message ?? data?.message ?? "no message";
			return {
				content: [{ type: "text", text: `The run is closed (${status}): ${message}. Stop now.` }],
				details: { accepted: false, closed: true },
				terminate: true,
			};
		},
	});

	pi.on("agent_end", async (event: any) => {
		if (accepted || closed || nudges >= MAX_NUDGES) return;
		const messages: any[] = event?.messages ?? [];
		const last = [...messages].reverse().find((message) => message?.role === "assistant");
		if (!last || last.stopReason !== "stop") return;
		nudges += 1;
		pi.sendUserMessage(
			"You have not submitted a verdict. Call submit_verdict now, with one answer per question label.",
			{ deliverAs: "followUp" },
		);
	});

	pi.on("tool_call", async (event: any) => {
		const name: string = event.toolName;
		const input: any = event.input ?? {};
		if (name === "write" || name === "edit") {
			const target = String(input.path ?? "");
			if (!target || !inside(target, WORK_DIR)) {
				return { block: true, reason: `${name} is allowed only inside ${WORK_DIR}` };
			}
		}
		if (READ_ROOTS.length > 0 && ["read", "grep", "find", "ls"].includes(name)) {
			const target = String(input.path ?? ".");
			const roots = [...READ_ROOTS, RUN_DIR];
			if (!roots.some((root) => inside(target, root))) {
				return { block: true, reason: `reading is allowed only inside: ${roots.join(", ")}` };
			}
		}
		if (name === "bash") {
			const requested = Number(input.timeout);
			input.timeout =
				Number.isFinite(requested) && requested > 0
					? Math.min(requested, BASH_TIMEOUT_SECONDS)
					: BASH_TIMEOUT_SECONDS;
		}
		return undefined;
	});

	if (REPLACE_BASH) {
		const bashTool = createBashTool(WORK_DIR, {
			spawnHook: ({ command, env }: any) => ({ command, cwd: WORK_DIR, env: scrubbed(env) }),
		});
		pi.registerTool({
			...bashTool,
			execute: async (id: string, params: any, signal: any, onUpdate: any) =>
				bashTool.execute(id, params, signal, onUpdate),
		});
	}
}
