# Pairing with a boardkit board

A wiki and a boardkit board coexist in the same family of repos because
they hold different facts. The board tracks planned work - cards,
statuses, gates. The wiki holds remembered work - append-only session
events, curated workstreams, the pending queue. Neither is the other's
shadow.

## Layout

Both use the same docking shape (the wiki's `.wiki/` dock is vendored
from boardkit's `.boardkit/` precedent; see `docking-spec.md`). A repo
may carry both docks: `.boardkit/` pointing at the board repo,
`.wiki/` pointing at the wiki repo. They do not share files, and
neither install step touches the other's.

## Which fact lives where

- A card's status, owner, and gates live on the board. The wiki's
  doctor `board` check reads board structure when a deployment runs
  one, but the board is the source (`DOCTOR-TRIAGE.md`).
- What happened in a session, and the curated state of a goal, live in
  the wiki: handoff events and workstream files.
- When a card and a workstream cover the same goal, the workstream is
  the memory of the work and the card is the plan for it; link between
  them rather than duplicating either fact.

## How the flows relate

The wiki's handoff -> garden -> render loop runs on session boundaries;
the board's card flow runs on work boundaries. A session that moves a
card typically also writes a handoff, and the board-hygiene step (close
out the cards you touched) pairs naturally with the handoff - but each
system is updated through its own tooling, and neither pipeline writes
the other's files.
