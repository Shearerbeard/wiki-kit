# Doctor triage

`wiki-doctor.py --wiki <root>` runs twelve checks in a fixed order and
prints `PASS` / `WARN` / `FAIL` per check. Exit code is 1 if any check
fails (or if any warns under `--strict-warnings`); `--json` emits the
same results machine-readably. The doctor observes and reports - it
never rewrites. What each layer of enforcement covers is stated in
`enforcement-contract.md`; this file is the per-check reading of the
output.

Checks in run order:

## config

The wiki's `wiki.toml` loads under the strict schema (unknown keys and
overlay keys outside the allowlist are load-time errors, printed as
`FAIL config` before the check list) and the committed file carries no
machine paths. Summary: companion count.

- FAIL "committed wiki.toml carries a machine path": move the value to
  `wiki.local.toml`. `[memory].index_line` is the one display-text
  exemption.

## render-log

`wiki/log.md` is re-rendered from the event store (plus epoch, legacy,
and quarantine inputs) and compared byte-for-byte.

- FAIL "differs; run the kit renderer (log)": run
  `wiki-render.py log`. Never hand-edit `wiki/log.md` to match.
- FAIL naming a read/parse error: an event or input file is unreadable
  or invalid; fix the named file, then re-render.

## validate-workstreams

Every workstream file (`workstreams/**/*.md`, `_reference/` and
`index.md` excluded, lock artifacts excluded) parses and passes the
frontmatter and body validators - the same validator the pre-commit
hook runs.

- FAIL naming a file: the message is the validator's; fix the
  frontmatter or body section it names.

## frontmatter-roundtrip

Each workstream file's frontmatter is in canonical writer form:
parse-then-format reproduces the file exactly.

- FAIL "not in canonical writer form": the file was hand-edited into a
  non-canonical shape (key order, spacing). Re-emit it with the kit's
  frontmatter writer rather than hand-aligning it.

## repo-names

Each distinct `repo:` value in workstream frontmatter is checked against
GitHub (`gh api`, 1h cache).

- FAIL "does not exist on GitHub": the workstream names a repo that
  404s; fix the frontmatter or add the companion. The finding lists the
  configured companion repos as a hint.
- WARN "could not verify" / "gh is not installed": network or tooling
  gap, not content drift; rerun when `gh` and the network are back.

## pending-index

The pending projection (`wiki/pending/index.json`, `latest.md`) is
rebuilt from events and sources and compared.

- FAIL "<mismatch>; run the pending builder": run
  `wiki-event.py build-pending`.
- The night runner verifies the same projection before it applies
  anything, through the same rebuild; a night report that aborts on
  "pending projection differs from the event store" while this check
  passes means the two rebuilds disagree, which is a kit defect, not
  content drift.

## token-budgets

Estimated token counts ((bytes + 3) / 4) against the deployment's
`[budgets]` (`wiki-toml-schema.md`); the defaults are `CLAUDE.local.md`
warn 2000 / hard 3000, the per-project memory index 1500/2000, each
workstream file 2500/4000, each entity page 2000/3500. The memory index
is budgeted only when present.

- FAIL on `CLAUDE.local.md`: the orientation outgrew its budget. The
  levers, cheapest first: set `[budgets].parallel_workstreams_target`
  so older active workstreams collapse to one-line rows; park or
  archive finished workstreams; tighten the Quickstart; raise the
  budget deliberately.

- WARN/FAIL: the named surface is too big. Garden the workstream or
  entity down; an over-budget `CLAUDE.local.md` means the orientation
  index needs curation, not a bigger budget.

## links

Markdown links in `README.md`, `CLAUDE.md`, `DECISIONS.md`,
`wiki/index.md`, and `wiki/entities/**` resolve to real files.
External links, anchors, and `mailto:`/`tel:`/`git@` targets are
skipped.

- FAIL "broken markdown link": fix or remove the link it names.

## install

Installer-owned state: the pre-commit hook exists and is the kit's
runtime-resolving wrapper, the overlay carries a `[tools] kit` path,
and the `.claude/settings.json` deny rules derived from `[contract]`
are all present.

- FAIL "hook is not installed" / "not the kit's wrapper" / "no [tools]
  kit path": run (or re-run) the kit installer.
- FAIL "missing Claude deny rule": `[contract]` changed or settings
  drifted; re-run the installer to re-derive the rules.
- WARN "git cannot locate the main checkout": exotic git layout; the
  pre-commit wrapper cannot resolve the overlay from here.

## captures

Every capture manifest under `wiki/sources/**` validates against its
schema, and every captured file still exists with the recorded sha256
and size.

- FAIL "captured file missing" / "sha256 mismatch" / "size mismatch":
  a content-addressed capture drifted or was deleted; restore it or
  re-capture. Captures are evidence - do not edit them to match.
- FAIL "manifest invalid": the manifest file itself fails schema
  validation.

## board

Optional. Skips cleanly when the wiki has no planning board. The board
location is `boardkit.toml`'s `[board].cards_dir` when present, else
`planning/board.md` + `planning/cards/`. Structural checks: the board
has `In progress` / `Ready` / `Done` sections, in-progress cards exist,
have an owner, and have Log lines, and done cards have no unchecked
gates.

- FAIL: fix the named board or card by hand - boards are curated state.

## kit-stamp

The `[kit]` stamp (contract version + kit commit) is compared with the
kit checkout the overlay points at.

- WARN "no [kit] stamp": pre-stamp install; re-run the installer.
- FAIL "contract version N is not supported": the deployment was
  installed from an incompatible kit; install from a kit whose contract
  matches (`VERSIONING.md`).
- WARN "stamped at X but the kit checkout is at Y": drift between the
  stamp and the kit in front of you; expected while a kit update is in
  flight, otherwise re-run the installer.
- WARN "recorded kit path is not a git checkout": the overlay's
  `[tools] kit` is stale; re-run the installer.
