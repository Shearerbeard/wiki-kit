# Review tooling

The standing rules for adversarial review of this kit's changes. This
is the genericized minimum - machine-specific pins and provider names
belong to the deployment's own runbook; this file is the portable core.

## The standing rule

Substantive diffs get adversarial review by a different model family
than the author's. The reviewer produces:

- a numbered findings ledger (each finding: severity, file:line,
  claim),
- an explicit verdict (PASS / FAIL per lane),
- and never a pass on unverified claims: a finding the reviewer did not
  check is reported as unchecked, and "looks right" without evidence is
  not a PASS.

Reviewer-differs-from-author is the point: same-family review re-derives
the author's blind spots.

## Transport

Drive the reviewer through its CLI, not an in-call transport. A CLI run
gives you a job handle you can poll, a deadline you own (a stall is a
failed run, not a slow pass), and a transcript you can file. In-call
transports hide all three.

## Stalls

A review job that misses its deadline is dead: stop it and report the
stall by name, keeping whatever partial output exists. Never retry
blindly - retry once with a diagnosed change (smaller packet, different
model, explicit output path) or record the deferral. A stalled review
blocks the gate it was covering; it does not get waived.

## Pre-vet the reviewer

Before trusting a reviewer lane with a real packet, send a
contract-shaped nonce probe: a small artifact with a planted, checkable
fact (a deliberate error the review contract requires catching). A
reviewer that passes the nonce packet without flagging the plant has
not earned the lane. Record the probe outcome next to the lane's
verdicts.

## Staging the packet

Stage review packets in a dedicated directory inside the repo
(`.review/` here, gitignored):

- Stage the dependencies, not just the diff: a reviewer who cannot read
  the code a diff touches will review the patch text and miss
  cross-file breakage. Include the files the diff depends on, or a map
  to them.
- Nothing outside the staging dir is readable by the reviewer job. This
  is both hygiene (no private strings leak into a third-party context)
  and discipline (the packet must be self-contained, which forces the
  dependencies rule above).
- The packet names the review contract: what to check, the findings
  format, the verdict shape.
