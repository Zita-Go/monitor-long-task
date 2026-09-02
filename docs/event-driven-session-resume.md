# Event-driven origin-session resume

## Goal

When a monitored task reaches a terminal state, deliver one Agent-authored message to the exact Codex thread that started it. If that thread is still running a turn, delivery must wait for Codex's formal `idle` state. Merely keeping the thread open or focused must not delay delivery.

## Invariants

- Identify the target only by the captured `CODEX_THREAD_ID`; never use `--last`.
- Treat only App Server `ThreadStatus.type == "active"` as busy.
- Do not infer activity from output timestamps, terminal text, log silence, or window focus.
- Never answer approval or user-input requests received by the observer connection; the interactive Codex client remains authoritative.
- Persist the delivery intent and a message hash before waiting.
- A monitor task may accept or spawn at most one origin-session delivery.
- Transport reconnection may retry after a disconnect, but thread state is not polled while a connection is healthy.
- Failure to resume the Codex thread never changes the monitored task's terminal result or Feishu notification result.

## Delivery paths

### Durable App Server queue

When the connected App Server supports `thread/queue/list` and `thread/queue/add`, the monitor uses a stable `clientUserMessageId` to find or add the delivery. The App Server persists the queued turn and starts it in FIFO order when the thread becomes idle. A reconnect checks for an existing pending entry before adding one. If the connection is lost after an add request but before its response, the monitor records an uncertain failure instead of retrying a mutation that could create a duplicate.

### Status-event compatibility path

Older App Server versions do not expose the durable queue API. The monitor then:

1. Calls `thread/resume` with `excludeTurns: true` to atomically subscribe to the exact loaded thread and obtain its current status.
2. Dispatches immediately when the returned status is `idle` or `notLoaded`.
3. When it is `active`, blocks on the WebSocket event stream until a matching `thread/status/changed` notification reports `idle`.
4. Starts one detached `codex exec resume <thread-id> -` process and passes the message over stdin.

If the App Server connection is lost, the monitor reconnects with bounded backoff and obtains one fresh status snapshot as part of restoring the subscription. This is transport recovery, not periodic thread-status polling.

### CLI compatibility path

Some standalone Codex CLI sessions do not expose a managed App Server Unix socket. If no Unix socket exists when the monitor task is created, the request records that the idle gate is unavailable and retains the previous one-shot `codex exec resume` behavior. The state file makes this fallback explicit.

## Persisted states

`origin_session_resume.status` uses these values:

- `waiting_for_idle`: the delivery intent is persisted and the event subscriber is connecting or waiting.
- `queued`: the App Server durable queue accepted the message (or already contained its stable client message ID).
- `dispatched`: the compatibility path started one `codex exec resume` process.
- `failed`: the idle deadline expired, the thread entered `systemError`, or delivery could not be accepted or spawned.

The resume message itself is not written to monitor state. Only its SHA-256 digest, target thread ID, timestamps, and routing diagnostics are stored.
