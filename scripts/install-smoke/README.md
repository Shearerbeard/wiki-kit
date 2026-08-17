# Install smoke

Dockerized end-to-end proof that the kit installs into a blank repo and
runs one full lifecycle, credential-free and with `--network none`.

```sh
scripts/install-smoke/run.sh
```

What it proves, in order:

1. Blank-repo boot: `install.sh --wiki` against an empty directory
   seeds `wiki.toml`, the content skeleton, the projections, the
   orientation skeleton, the pre-commit hook wrapper, and the initial
   commit (the charter decision-4 boot floor).
2. Contract consolidation: the deny-rule assertion reads
   `[contract].protected` from the fixture's own `wiki.toml` and
   derives the expected rules, the same way the installer and doctor
   do. No hardcoded rule list exists in this harness.
3. Doctor clean (`--strict-warnings`) on the fresh install.
4. One full handoff -> garden -> render cycle through the real CLIs,
   committed through the hook.
5. Enforcement probes: one seeded violation per enforcement class
   (hand-edited log, modified event, stale pending, invalid event,
   invalid workstream), each of which must be BLOCKED at commit time.
   These are shell-mediated writes, so they also demonstrate the
   layer-1/layer-2 boundary in docs/enforcement-contract.md.
6. Idempotent reinstall: a second run changes nothing and leaves the
   tree clean.
7. The decision-4 tweak: installing into a fixture that already carries
   `docs/`, a `.obsidian/` vault, and a README leaves every
   pre-existing file byte-untouched.

Reports land in `reports/install-smoke/` (`latest.log`, `latest.json`,
`latest.md`); the directory is gitignored run output. Python inside the
container is the system `python3` with the apt `python3-jsonschema`
(installed at image build; the run itself is offline); the uv-managed
pinned environment is exercised by pytest outside Docker.

Environment overrides: `IMAGE_NAME`, `CONTAINER_NAME`,
`KEEP_CONTAINER=1` to keep a passing container for inspection.
