// foundation-models-summarizer
//
// Optional on-device helper, NOT built or invoked by default. Wraps Apple
// Intelligence's Foundation Models framework (macOS 15+/26, Apple Silicon)
// for tasks the JXA write layer cannot do: summarizing an email thread,
// drafting a short reply suggestion. Reached from Python via `subprocess`
// (see src/cobos_apple_mail_mcp/read/llm_helper.py) rather than PyObjC,
// because FoundationModels is a Swift-only framework — PyObjC can only
// bridge Objective-C APIs.
//
// Protocol: reads one JSON object from stdin, writes one JSON object to
// stdout, exits. No daemon, no persistent state — a fresh process per
// call, consistent with the rest of this project's "every external call is
// a bounded subprocess" rule (CLAUDE.md invariant #4).
//
//   stdin:  {"task": "summarize", "text": "..."}
//   stdout: {"summary": "..."}              on success
//           {"error": "..."}                on failure (model unavailable,
//                                            guardrail refusal, etc.)
//
// Token budget: Foundation Models' on-device session has a ~4096-token
// combined input+output window; callers should keep `text` to a few
// thousand characters (a thread summary, not a whole mailbox).

import Foundation
import FoundationModels

struct Request: Decodable {
    let task: String
    let text: String
}

struct SuccessResponse: Encodable {
    let summary: String
}

struct ErrorResponse: Encodable {
    let error: String
}

@available(macOS 26.0, *)
func summarize(_ text: String) async throws -> String {
    let session = LanguageModelSession(
        instructions: """
        You summarize email content concisely and factually. Do not invent
        facts not present in the text. Keep summaries under 3 sentences.
        """
    )
    let prompt = "Summarize this email thread:\n\n\(text)"
    let response = try await session.respond(to: prompt)
    return response.content
}

func writeJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    if let data = try? encoder.encode(value), let json = String(data: data, encoding: .utf8) {
        print(json)
    } else {
        print(#"{"error": "failed to encode response"}"#)
    }
}

func readStdin() -> String {
    var input = ""
    while let line = readLine(strippingNewline: false) {
        input += line
    }
    return input
}

@available(macOS 26.0, *)
func run() async {
    let raw = readStdin()
    guard let data = raw.data(using: .utf8),
        let request = try? JSONDecoder().decode(Request.self, from: data)
    else {
        writeJSON(ErrorResponse(error: "invalid JSON on stdin; expected {\"task\":..., \"text\":...}"))
        return
    }

    guard request.task == "summarize" else {
        writeJSON(ErrorResponse(error: "unsupported task: \(request.task)"))
        return
    }

    do {
        let summary = try await summarize(request.text)
        writeJSON(SuccessResponse(summary: summary))
    } catch {
        writeJSON(ErrorResponse(error: "Foundation Models request failed: \(error.localizedDescription)"))
    }
}

if #available(macOS 26.0, *) {
    let semaphore = DispatchSemaphore(value: 0)
    Task {
        await run()
        semaphore.signal()
    }
    semaphore.wait()
} else {
    writeJSON(ErrorResponse(error: "Foundation Models requires macOS 26 or later"))
}
