# Adopting the kit: docking a consumer repo

This is the end-to-end path from "I have the kit checked out" to "this
consumer repo has a working wiki dock". Everything below runs from the
kit checkout; replace `/path/to/kit` with yours.

## 1. Install the wiki (or point at an existing one)

New wiki:

```sh
/path/to/kit/scripts/install.sh --wiki /path/to/wiki
```

Works on a blank directory, an existing git repo, or a directory with
docs and an Obsidian vault already in it; pre-existing content is never
touched, and reinstalling is idempotent. The installer also renders
scheduler units for the night, morning, and garden-reminder jobs
(launchd on macOS, systemd user timers on Linux; `--no-scheduler`
skips). Schedule times and the rest of the config surface are documented
in `wiki-toml-schema.md`.

Already have a wiki installed? Skip to step 2.

## 2. Declare the consumer as a companion

In the wiki's `wiki.toml`, add one table per consumer repo:

```toml
[companions.widget]
posture = "committed"            # see the posture table below
docs_subpath = "docs/wiki-outbox"  # optional; enables the post-commit
                                   # hook and the opencode handoff plugin
```

The companion table is the single home for everything semantic about
this consumer (docking-spec.md); the wiki's gitignored
`wiki.local.toml` overlay may carry its machine path.

## 3. Dock the consumer

```sh
/path/to/kit/scripts/wiki-dock.py install \
  --wiki /path/to/wiki \
  --repo /path/to/consumer \
  --companion widget \
  --skills-dir .agents/skills \
  --skills-dir .claude/skills
```

Pick a posture once, in the companion table (or pass `--posture`; the
flag must not contradict the table):

| Posture | Tracked in the consumer | Visible in `git status` | For |
|---|---|---|---|
| `committed` | `.wiki/manifest.toml`, `AGENTS.md` block, `CLAUDE.md` shim | nothing after commit | repos that acknowledge the wiki openly |
| `gitignored` | tracked `.gitignore` lines only | nothing | repos that admit a dock may exist without carrying one |
| `invisible` | nothing | nothing | OSS-bound repos; exclusion via `.git/info/exclude` |

The semantics and the resolution order are ratified in
`docking-spec.md`; this table is the summary, not the source.

`--skills-dir` chooses which repo-relative directories the four workflow
skills (`garden`, `handoff`, `morning`, `session-feedback`) render into.
Choose per the harnesses you use: `.agents/skills/` covers pi, opencode,
and codex; `.claude/skills/` covers claude-code and opencode. Repeatable;
project-scoped only - machine-global paths are refused.

## 4. What install writes

- `.wiki/manifest.toml` - identity (wiki name + companion), tracked in
  committed posture.
- `.wiki/local.toml` - machine-local overlay with the wiki path; never
  committed in any posture.
- `.wiki/orientation.md` - the rendered cold-start orientation; never
  committed in any posture (it embeds the machine-local wiki root).
- `AGENTS.md` - a marker-delimited dock block
  (`<!-- wiki-kit:dock:start -->` / `:end -->`) appended to an existing
  file, or the file created if absent. Text outside the markers is
  preserved byte-exact; the marked region is kit-owned and replaced on
  reinstall.
- `CLAUDE.md` - created as a one-line shim pointing at `AGENTS.md` if
  absent; the dock block is appended if it exists without an AGENTS.md
  pointer; left alone if it already points at `AGENTS.md`.
- With `docs_subpath`: the post-commit hook wrapper and the opencode
  handoff plugin.
- With `--skills-dir`: the rendered skills plus
  `.wiki/rendered-skills.json`, the kit's provenance record of what it
  wrote (never committed).

Foreign files are never clobbered: a hand-written `AGENTS.md`, a
non-kit post-commit hook, or a skill file with no provenance entry is
left in place, and a conflict on the hook fails the install before any
write lands.

## 5. Verify

```sh
python3 /path/to/kit/scripts/wiki-doctor.py --wiki /path/to/wiki
/path/to/kit/scripts/wiki-dock.py status --repo /path/to/consumer
```

Doctor checks are triaged in `DOCTOR-TRIAGE.md`. To prove a harness
sees the dock, run the probe:

```sh
python3 /path/to/kit/scripts/wiki-probe.py \
  --repo /path/to/consumer --harness all
```

Each probe drives one harness headlessly and grades whether it names
the wiki and sees the rendered skills; transcripts land in the
consumer's `.wiki/probes/`.

## 6. Reinstalls and upgrades

Everything above is idempotent: re-running install over an existing
dock re-renders what changed and reports "up to date" for the rest. A
fresh clone of a committed-posture consumer re-runs the same install
command to recreate the machine-local files (overlay, orientation,
skills); the tracked manifest proves identity. An incomplete dock
(manifest without overlay, the normal state of a linked worktree) is
completed by `wiki-dock.py complete`, the command the resolver's error
message names.

What a consumer session reads at cold start: `AGENTS.md` (or the
`CLAUDE.md` shim) points at `.wiki/orientation.md`, which names the
wiki root, the rendered skills, and the commands a session needs.
