# The `.wiki/` docking spec

Status: RATIFIED at K1's Gate U, 2026-08-16 (board `wk`). The kit is
`wiki-kit` per the decision-1 ruling; the `.wiki/` dock name stands.
Date: 2026-08-16. The convention is vendored from boardkit's proven
`.boardkit/` shape per reserved decision 3's proposal; if the boardkit
maintainer ships a shared docking library, K10 swaps the implementation
and this spec stays the contract. Source direction: Mike's 2026-08-11
`dotname-docking-generalization` entry in boardkit's `FEEDBACK.md`
("resolution is computed, not stored"; the `CLAUDE.local.md` symlink is
the stored-link pattern to retire).

## What the dock is

A consumer repo docks to a wiki by carrying a `.wiki/` directory. The
dock points; the wiki holds. Machinery lives in the kit repo, content
and config live in the wiki repo, and the dock holds only enough to find
them: an identity manifest plus a machine-local overlay. The dir name is
`.wiki/` regardless of what decision 1 names the kit, matching the
`.boardkit/` precedent where the dot-dir names the domain, not the tool.

## Resolution order

First hit wins. Every kit CLI and skill resolves the wiki root this way;
no tracked file ever stores an absolute path to it. Each step's input
names one specific thing - the two override channels denote different
directories on purpose, matching what each caller holds:

1. Explicit `--wiki <path>` flag: `<path>` is the WIKI REPO ROOT itself
   (the directory containing `wiki.toml`). Bypasses dock discovery
   entirely.
2. `WIKI_DOCK` environment variable: the DOCK DIRECTORY (a `.wiki/`
   dir, or a directory directly containing one); the wiki root then
   comes from that dock's manifest + overlay.
3. Walk-up: from the working directory toward the repository toplevel
   (`git rev-parse --show-toplevel`), the first directory containing
   `.wiki/` is the dock; resolve as in step 2. The walk never leaves
   the repository, and outside any git repo there is no walk-up at all
   - a dock lives inside a repo by definition, so an out-of-repo caller
   reaches a wiki only through the flag, env, or legacy channels. The
   bound exists so a worktree or undocked directory can never silently
   resolve an unrelated ancestor's `.wiki/`. Inside the wiki repo
   itself there is no dock - walk-up finds `wiki.toml` directly and the
   containing directory is the root.
4. Git common-dir fallback: in a linked worktree, `git rev-parse
   --git-common-dir` locates the main checkout, and its `.wiki/`
   resolves as in step 2. A worktree needs zero per-worktree setup;
   this replaces the `post-checkout` symlink-recreation hook.
5. Legacy fallback: the `AURA_WIKI`-style env default and the
   `CLAUDE.local.md` symlink convention, honored read-only so the live
   aura install keeps working between K2 and K11. Retired at an
   adoption K10 rules to proceed.

An INCOMPLETE dock - manifest present, overlay missing - does not stop
resolution, because that is the normal state of a committed-posture
linked worktree: the tracked manifest checks out, the gitignored
overlay exists only where the dock install step ran. Step 3 finding an
incomplete dock falls through to step 4, and only when no step yields a
complete dock does resolution fail loud, naming the incomplete dock and
the command that creates its overlay. What never happens is silent
fall-through PAST a complete dock, or resolution of a wiki the nearest
manifest does not name - the identity chain below still binds whichever
dock completes.

## Manifest and overlay

- `.wiki/manifest.toml` - trackable (whether it is actually committed is
  the posture, below). Identity only, two keys:

  ```toml
  [dock]
  wiki = "acme-notes"     # must equal the wiki repo's [wiki].name
  companion = "widget"    # must name a [companions.<name>] table there
  ```

  The `companion` key is how a resolved dock selects its own
  configuration: installer, doctor, and renderer look up
  `[companions.<companion>]` in the resolved wiki's config, so the
  selection is deterministic however many companions the wiki tracks.
  Everything semantic about this consumer (docs outbox subpath, display
  label, posture) lives in that one table, so no fact has two homes and
  no precedence question arises.
- `.wiki/local.toml` - never committed, listed in the posture's ignore
  mechanism. One allowlisted key:

  ```toml
  [dock]
  path = "/abs/path/to/the-wiki-repo"
  ```

  Any other key in the overlay is a doctor error. The doctor also
  verifies the identity chain both ways: the `wiki.toml` found at
  `path` must carry the `[wiki].name` the manifest claims, and that
  config must contain the `[companions.<name>]` table the manifest's
  `companion` key names - either mismatch fails loud naming both
  values.

The hard rule inherited from boardkit (RULE-2): no machine paths in any
tracked file. A committed manifest with an absolute path fails the kit's
doctor.

## Generated surfaces live in the dock

The orientation file for a consumer lands inside the dock (as
`.wiki/orientation.md`), never committed in any posture (it embeds the
machine-local wiki root), rendered by `wiki-dock install` and
re-rendered by `wiki-dock complete` from the kit's
`templates/orientation.md.template`. Harness entry files in the
consumer repo (`CLAUDE.md`, `AGENTS.md`) carry
a one-line pointer at that surface, following boardkit's entry-shim
pattern. The pointer text instructs resolution through the dock rather
than encoding a literal relative path, because a linked worktree has no
`.wiki/` of its own - the reader (or the skill it loads) reaches the
main checkout's dock through the common-dir fallback, the same gap the
`post-checkout` hook papers over today with a per-worktree symlink.
In the kit's design the `CLAUDE.local.md` symlink pattern is retired:
the stored link becomes a computed resolution plus a stable in-dock
file. The live aura deployment's own symlink and `post-checkout` hook
retire only at an adoption K10 rules to proceed, via the legacy
fallback above. The per-harness wiring and supported-harness set ruled
under reserved decision 11 ship as follows: AGENTS.md carries a
marker-delimited dock block (`<!-- wiki-kit:dock:start -->` /
`:end -->`) appended by dock install - the marked region is kit-owned
and replaced on reinstall, text outside the markers is preserved
byte-exact, and an absent AGENTS.md is created containing only the
block. CLAUDE.md carries the one-line shim at AGENTS.md: created if
absent, the dock block appended if present without an AGENTS.md
pointer, left alone if it already points at AGENTS.md. The supported
harnesses are pi, opencode, claude-code, and codex;
`scripts/wiki-probe.py` is the scripted proof that each reads the
orientation and sees the rendered skills.

Entry shims follow the posture. Committed posture: shims tracked.
Gitignored posture: shims untracked, covered by the same tracked
`.gitignore` lines as the dock, generated per-clone by the dock install
step. Invisible posture: shims untracked via `.git/info/exclude`,
generated per-clone. The untracked postures preserve exactly today's
`CLAUDE.local.md` behavior - a machine-generated orientation file that
exists on working clones and never appears in shared history.

Generated surfaces are read through the RESOLVED dock, never through a
literal path relative to the reader's checkout: a linked worktree has
no `.wiki/orientation.md` of its own, and its reader reaches the main
checkout's copy via the common-dir fallback above. And the posture's
exclusion set covers everything the dock install step writes, not just
`.wiki/` - in an invisible-posture repo, project-scoped skill installs
and generated shims are added to `.git/info/exclude` by the same
install step, so nothing wiki-related ever surfaces in `git status`.

## Consumer postures

Per-repo, chosen at docking time, recorded in the wiki repo's
`wiki.toml` companion table:

- **Committed**: `.wiki/manifest.toml` and the entry shims are tracked;
  `.wiki/local.toml` and generated files are in `.gitignore`. For repos
  that acknowledge the wiki openly (chore-lottery's expected posture,
  per decision 8's proposal).
- **Gitignored**: the whole `.wiki/` dir is in the repo's tracked
  `.gitignore`; shims are not committed. The repo admits a dock may
  exist without carrying one.
- **Invisible**: `.wiki/` is excluded per-clone via `.git/info/exclude`;
  nothing wiki-related is tracked or visible in shared history. For
  OSS-bound repos (the `mezmo/aura` clone). Scale-up note carried from
  boardkit's proposal: a second adopter on the same repo promotes
  invisible to a tracked `.gitignore` line as a deliberate step.

Which repo gets which posture is reserved decision 8, ruled at K3.

## What this spec does not cover

Scheduler units, deny-rule injection, and skill installation are
installer concerns (K2/K3), configured through `wiki.toml`
(`wiki-toml-schema-2026-08-16.md`). The wiki repo's own config pair
(`wiki.toml` committed + `wiki.local.toml` overlay) mirrors the
manifest/overlay split described here but lives at the wiki root, not in
a dock; a wiki does not dock to itself.
