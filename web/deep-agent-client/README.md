# Deep Agent React Client

Two files to drop into the existing React and TypeScript application:

- `useDeepAgentThread.ts`: the hook. Streams `POST /threads/{id}/messages` and
  `POST /threads/{id}/resume` as Server Sent Events over `fetch`, not
  `EventSource` (the browser API is GET only; these endpoints are POST).
- `DeepAgentChatExample.tsx`: an unstyled usage example, not a component meant
  to ship as is.

## Contract

See `HANDOFF.md` in the project root, Contract 1, for the authoritative
`action_requests` / `review_configs` / decision shapes. This client is a
direct implementation of that contract; `tests/test_service_sse.py` pins the
exact wire format on the Python side.

## Not yet verified

This code has not been exercised against a running instance of
`service/app.py` or bundled through the actual React/TypeScript build in this
session -- there was no frontend project in this repository to run it
against. Before shipping, wire it into the real app and drive one approve and
one reject through the UI by hand.
