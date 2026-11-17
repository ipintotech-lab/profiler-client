# Working rules

## Event format changes

- Release order: when the event format changes (`profiler_client/contract.py`,
  the server's `TraceEvent`, or the seal), the server that accepts the change
  deploys first. Only once it is live is a client release that emits it tagged.
  The server validates uploads strictly (unknown fields are refused), so a
  client that emits a field the live server doesn't know gets its uploads
  refused.
- Propose any event format change to the project owner and wait for their OK
  before building it.
