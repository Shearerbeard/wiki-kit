---
status: active
branch: main
sha: abc1234
last_updated: 2026-05-24
session_id: pending
blocker: ""
---

## Eval Benchmark — Tool Call Audit

### Current State
- 7 models x 3 checkpoints complete
- Tool call audit done: 68-call structural floor
- Scope leakage fix tested: Qwen dropped 141 to 89 tools
- /benchmark-viz skill prototype working

### What Was Done
#### 2026-05-24 (session 2 — viz skill)
1. Build `/benchmark-viz` skill
2. Tested on 3 model runs, output matches hand-drawn diagrams

#### 2026-05-24 (session 1 — scope leakage fix)
1. Removed TASK_GOAL_DIRECTIVE from worker_task_prompt.md
2. Re-ran Qwen td24 on E: tool count 141 to 89 (37% reduction)
3. All 5 prompts still pass — no quality regression

#### 2026-05-17 (previous session)
1. Gemini E off-peak: 100% quality, 74 tools, 377k tokens
2. Tool call audit: all 7 models x 5 prompts parsed
3. Qwen deep-dive: over-execution + all-namespace exploration
4. Scope leakage root cause found

### Next
1. Build `/benchmark-viz` skill
2. Verify all 7 models pass without TASK_GOAL_DIRECTIVE
3. Token accuracy investigation (Sonnet 1.8M, Opus 1.6M seem inflated)
4. E + scratchpad run (GPT-5.5)

### Continuation Context
Scope leakage fix validated on Qwen. Viz skill working. Need to verify remaining 6 models without TASK_GOAL_DIRECTIVE before merging the prompt change. Token accuracy still uninvestigated.
