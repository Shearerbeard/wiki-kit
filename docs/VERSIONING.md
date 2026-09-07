# Versioning

## The contract stamp

Every installed deployment carries a `[kit]` table in its `wiki.toml`:

```toml
[kit]
contract_version = 1
commit = "<kit commit the installer ran from>"
```

`contract_version` is the deployment contract version - the shape of
what the installer writes and the tools read (config schema, hook
wrapper, projections, dock). `commit` records which kit checkout
stamped it. The installer's `CONTRACT_VERSION` is `1` today;
`SUPPORTED_CONTRACT_VERSIONS` in `scripts/wiki_config.py` is the set a
kit build accepts.

The doctor's `kit-stamp` check is the drift tripwire: an unsupported
contract version FAILs, and a stamp commit that differs from the kit
checkout in front of you WARNs (expected while an update is in flight;
otherwise re-run the installer). Supported-ness is a doctor finding,
not a load-time crash, so an unknown future version does not break
every tool against the deployment.

## How upgrades land

There are no release tags yet; the kit moves by commit on its working
branch. Upgrading a deployment is:

1. Update the kit checkout.
2. Re-run the installer against the wiki (`install.sh --wiki ...`) -
   idempotent: it rewrites what changed, stamps the new commit, and
   reports the rest up to date.
3. Re-run `wiki-dock.py install` on each docked consumer - the
   orientation, entry-file blocks, and rendered skills re-render the
   same way, with foreign files never clobbered.

A contract version bump, when one is needed, is a deliberate migration:
the new version lands in `SUPPORTED_CONTRACT_VERSIONS` alongside `1`
until deployments are re-stamped, and the doctor names the mismatch in
the meantime.

## What counts as a contract bump

Two classes of config change, told apart by who writes the table. A
change to what the installer writes, or to what the tools read from an
installer-written table, is a contract bump: the new version lands in
`SUPPORTED_CONTRACT_VERSIONS` beside the old one, the installer's
`CONTRACT_VERSION` moves, and the doctor's `kit-stamp` names the
mismatch until every deployment is re-stamped.

A rename inside an optional, hand-authored table is not a bump.
`[budgets]` is such a table: the installer never writes it. Nor could
a re-stamp repair a refused key, because the installer loads the
config before it stamps. The loader refuses the old key with a
`ConfigError` that names its replacement. The roll-out inventories
every docked deployment's copy of the table and moves it in the same
change that updates the kit checkout there, and the card that made the
change records that inventory. The first rename of this kind is the
`[budgets]` reshape in `docs/adr/0012-token-budgets.md`, where the
Stage 1 spelling (`claude_local_hard` and its siblings, plus
`parallel_workstreams_target`) gives way to `<surface>_tokens` and
`workstreams_in_view`.
