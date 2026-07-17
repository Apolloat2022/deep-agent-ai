/**
 * Minimal usage example for useDeepAgentThread.
 *
 * Not a styled component -- drop this logic into your existing chat UI.
 * It exists to show the approval flow end to end: render pending
 * action_requests, let the reviewer approve or reject each one, and POST
 * the decisions in the same order the server sent the requests.
 */

import { useState } from "react";
import { useDeepAgentThread } from "./useDeepAgentThread";

export function DeepAgentChatExample({
  baseUrl,
  threadId,
}: {
  baseUrl: string;
  threadId: string;
}) {
  const [input, setInput] = useState("");
  const {
    streamedText,
    finalText,
    pendingInterrupt,
    isStreaming,
    error,
    sendMessage,
    resume,
  } = useDeepAgentThread({ baseUrl, threadId });

  const approveAll = () => {
    if (!pendingInterrupt) return;
    resume(pendingInterrupt.action_requests.map(() => ({ type: "approve" })));
  };

  const rejectAll = () => {
    if (!pendingInterrupt) return;
    resume(
      pendingInterrupt.action_requests.map(() => ({
        type: "reject",
        message: "rejected by reviewer",
      })),
    );
  };

  return (
    <div>
      <div>{finalText ?? streamedText}</div>

      {pendingInterrupt && (
        <div>
          <p>Approval required:</p>
          <ul>
            {pendingInterrupt.action_requests.map((action, i) => (
              <li key={i}>
                {action.name}: {JSON.stringify(action.args)}
              </li>
            ))}
          </ul>
          <button onClick={approveAll}>Approve all</button>
          <button onClick={rejectAll}>Reject all</button>
        </div>
      )}

      {error && <p role="alert">{error}</p>}

      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        disabled={isStreaming || !!pendingInterrupt}
      />
      <button
        onClick={() => sendMessage(input)}
        disabled={isStreaming || !!pendingInterrupt || !input}
      >
        Send
      </button>
    </div>
  );
}
