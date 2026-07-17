/**
 * React hook for the deep agent service's SSE contract.
 *
 * Talks to the three endpoints in `service/app.py`:
 *   POST /threads/{id}/messages  -- send a user turn, stream the run
 *   POST /threads/{id}/resume    -- resume a paused run with decisions
 *   GET  /threads/{id}/state     -- recover a pending interrupt on reconnect
 *
 * Both POST endpoints stream Server Sent Events in response to a POST body,
 * so the browser's built in `EventSource` (GET only) cannot be used. This
 * hook reads the streamed `fetch` response body directly and splits it on
 * SSE frame boundaries.
 *
 * The interrupt and decision shapes below are Contract 1 from HANDOFF.md,
 * sourced from the deepagents library's own HITL tests and middleware, not
 * guessed. Do not change field names here without checking that file.
 */

import { useCallback, useRef, useState } from "react";

export interface ActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

export interface ReviewConfig {
  action_name: string;
  allowed_decisions: ("approve" | "edit" | "reject" | "respond")[];
  args_schema?: Record<string, unknown>;
}

export interface PendingInterrupt {
  action_requests: ActionRequest[];
  review_configs: ReviewConfig[];
}

export type Decision =
  | { type: "approve" }
  | { type: "edit"; edited_action: { name: string; args: Record<string, unknown> } }
  | { type: "reject"; message?: string }
  | { type: "respond"; message: string };

export interface UseDeepAgentThreadOptions {
  /** Base URL of the deep agent service, e.g. "https://agents.internal.example.com". */
  baseUrl: string;
  /** Stable LangGraph thread id for this conversation. */
  threadId: string;
  /** Extra headers to send on every request, e.g. an auth bearer token. */
  headers?: Record<string, string>;
}

export interface UseDeepAgentThreadResult {
  /** Text streamed so far for the run currently in flight, or the last run. */
  streamedText: string;
  /** Set once a run ends without pausing on an interrupt. */
  finalText: string | null;
  /** Set when the run paused on a gated tool call; null otherwise. */
  pendingInterrupt: PendingInterrupt | null;
  /** True while a run (message or resume) is streaming. */
  isStreaming: boolean;
  /** Last transport or server error, if any. */
  error: string | null;
  /** Send a new user message. Rejects with a clear error if the thread is
   *  currently awaiting approval -- call resume() first in that case. */
  sendMessage: (content: string) => Promise<void>;
  /** Resume a paused run with one decision per pending action, in order. */
  resume: (decisions: Decision[]) => Promise<void>;
  /** Recover a pending interrupt after a page reload, without replaying
   *  the stream. */
  refreshState: () => Promise<void>;
}

interface ParsedEvent {
  event: string;
  data: unknown;
}

/** Split a raw SSE response body into typed frames. */
function parseSseText(raw: string): ParsedEvent[] {
  const events: ParsedEvent[] = [];
  for (const block of raw.split("\n\n")) {
    if (!block.trim()) continue;
    let eventName = "message";
    let dataLine = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) {
        eventName = line.slice("event: ".length);
      } else if (line.startsWith("data: ")) {
        dataLine = line.slice("data: ".length);
      }
    }
    if (dataLine) {
      events.push({ event: eventName, data: JSON.parse(dataLine) });
    }
  }
  return events;
}

export function useDeepAgentThread(
  options: UseDeepAgentThreadOptions,
): UseDeepAgentThreadResult {
  const { baseUrl, threadId, headers } = options;
  const [streamedText, setStreamedText] = useState("");
  const [finalText, setFinalText] = useState<string | null>(null);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(
    null,
  );
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a stale response finishing after a newer call started.
  const requestSeq = useRef(0);

  const consume = useCallback(
    async (path: string, body: unknown) => {
      const mySeq = ++requestSeq.current;
      setIsStreaming(true);
      setError(null);
      setStreamedText("");
      setFinalText(null);

      let response: Response;
      try {
        response = await fetch(`${baseUrl}${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...headers },
          body: JSON.stringify(body),
        });
      } catch (err) {
        if (mySeq === requestSeq.current) {
          setError(err instanceof Error ? err.message : String(err));
          setIsStreaming(false);
        }
        return;
      }

      if (response.status === 409) {
        if (mySeq === requestSeq.current) {
          setError("thread is awaiting approval; call resume() first");
          setIsStreaming(false);
        }
        return;
      }
      if (!response.ok || !response.body) {
        if (mySeq === requestSeq.current) {
          setError(`request failed with status ${response.status}`);
          setIsStreaming(false);
        }
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // The SSE spec delimits frames with a blank line ("\n\n"); a network
      // chunk boundary can land in the middle of a frame, so frames are
      // buffered until a full "\n\n" separated block is available before
      // parsing, and the plain-text `response.text()` path is never used.
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (mySeq === requestSeq.current) {
            for (const { event, data } of parseSseText(block + "\n\n")) {
              if (event === "token") {
                setStreamedText((prev) => prev + (data as { text: string }).text);
              } else if (event === "interrupt") {
                setPendingInterrupt(data as PendingInterrupt);
              } else if (event === "done") {
                setFinalText((data as { content: string }).content);
                setPendingInterrupt(null);
              }
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }

      if (mySeq === requestSeq.current) {
        setIsStreaming(false);
      }
    },
    [baseUrl, headers],
  );

  const sendMessage = useCallback(
    (content: string) => consume(`/threads/${threadId}/messages`, { content }),
    [consume, threadId],
  );

  const resume = useCallback(
    (decisions: Decision[]) => consume(`/threads/${threadId}/resume`, { decisions }),
    [consume, threadId],
  );

  const refreshState = useCallback(async () => {
    try {
      const response = await fetch(`${baseUrl}/threads/${threadId}/state`, {
        headers,
      });
      if (!response.ok) {
        setError(`state request failed with status ${response.status}`);
        return;
      }
      const body = (await response.json()) as {
        awaiting_approval: boolean;
        interrupt: PendingInterrupt | null;
      };
      setPendingInterrupt(body.awaiting_approval ? body.interrupt : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [baseUrl, headers, threadId]);

  return {
    streamedText,
    finalText,
    pendingInterrupt,
    isStreaming,
    error,
    sendMessage,
    resume,
    refreshState,
  };
}
