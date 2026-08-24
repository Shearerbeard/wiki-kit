---
status: active
branch: main
sha: abc1234
last_updated: 2026-05-17
session_id: pending
blocker: ""
---

## Eval Benchmark — Tool Call Audit

### Current State
- 7 models x 3 checkpoints complete
- Tool call audit done: 68-call structural floor
- Scope leakage root cause found (TASK_GOAL_DIRECTIVE)

### What Was Done
#### 2026-05-17 (previous session)
1. Gemini E off-peak: 100% quality, 74 tools, 377k tokens
2. Tool call audit: all 7 models x 5 prompts parsed
3. Qwen deep-dive: over-execution + all-namespace exploration
4. Scope leakage root cause found

### Next
1. Build `/benchmark-viz` skill
2. Test TASK_GOAL_DIRECTIVE removal on Qwen
3. Token accuracy investigation (Sonnet 1.8M, Opus 1.6M seem inflated)

### Continuation Context
Root cause identified. Next session: prototype the viz skill and test the prompt change on Qwen before rolling it out to all models.

## Session updates (uncurated)

_Appended mechanically by garden apply (one block per handoff event). Interactive /garden absorbs blocks into the curated sections above with user approval, then prunes them._
