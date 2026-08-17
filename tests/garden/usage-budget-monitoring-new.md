---
status: active
branch: alex/usage-budget-monitoring
sha: abc1234
issue: 270
last_updated: 2026-05-24
session_id: pending
blocker: ""
---

## GH #270 — Usage Budget Monitoring

### Current State
- Design complete for per-worker context budget streaming events
- Prototype: `telemetry.context_budget` event with tokens_used/tokens_available per turn
- GH issue #270 filed

### What Was Done
#### 2026-05-24 (this session)
1. Designed event schema: `agent_id`, `tokens_used`, `tokens_available`, `utilization_pct`
2. Identified insertion point: `on_stream_completion_response_finish` hook (same as scratchpad)
3. Added to `telemetry-events` crate design (new `ContextBudgetEvent` struct)
4. Filed GH #270

### Next
1. Implement in orchestrator.rs worker streaming hook
2. Add `ContextBudgetEvent` to `telemetry-events` crate
3. Wire into CLI streaming display
4. Integration test with known context_window config

### Blockers
None

### Continuation Context
New workstream. Design at GH #270. Insertion point identified. TASK: implement the event emission in the worker hook. HAZARDS: must not double-count with existing scratchpad_usage event.
