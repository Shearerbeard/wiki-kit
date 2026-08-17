"""The K2 acceptance grep sweep, as a test that runs on every suite run.

The kit repo boundary (charter, "Repo boundary"): zero source-family
strings outside the documented v1 legacy shim and its tests. The shim
exists because immutable v1 events on disk carry a JSON key literally
named "aura"; that key travels as a documented constant, never as
config.

Two exclusion classes, each with its recorded reason:

- ENCLAVE files implement or exercise the v1 legacy shim. Inside them,
  every line that mentions the family string must sit in shim context;
  the per-file assertions below pin that down so the enclave cannot
  quietly widen.
- PROVENANCE records under docs/ name their sources by design: the
  charter's decision-10 ruling makes the extraction ledger the
  provenance record, and a provenance record that cannot name its
  source is not one. They are dispositioned records, not machinery.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]

# Patterns the machinery must not contain. The \b guards keep
# LOG_PATH-style identifiers from tripping the ticket-prefix pattern.
FORBIDDEN = (
    re.compile(r"aura", re.IGNORECASE),
    re.compile(r"mshearer", re.IGNORECASE),
    re.compile(r"mezmo", re.IGNORECASE),
    re.compile(r"/Users/"),
    re.compile(r"\bLOG-[0-9]+\b"),
    re.compile(r"notanton", re.IGNORECASE),
)

# The v1 legacy shim and its tests: the one allowed machinery enclave.
ENCLAVE = {
    "scripts/wiki_event.py",
    "schemas/events/handoff-v1.schema.json",
    "tests/wiki-event/test_wiki_event.py",
    "tests/sweep/test_zero_aura_strings.py",  # this file names the terms
}

# Ratified provenance records merged at K1; they name their sources by
# design (decision 10: the ledger IS the provenance record).
PROVENANCE = {
    "docs/charter.md",
    "docs/extraction-ledger.md",
    "docs/docking-spec.md",
    "docs/wiki-toml-schema.md",
}

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".vale", "reports"}


def kit_files() -> list[Path]:
    # A symlink's "content" is its target's, checked once at the target.
    return [
        path
        for path in KIT_ROOT.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(
            part in SKIP_DIRS for part in path.relative_to(KIT_ROOT).parts
        )
    ]


def hits(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []  # binary; none of the machinery is binary today
    return [
        (lineno, line.strip())
        for lineno, line in enumerate(text.splitlines(), 1)
        if any(pattern.search(line) for pattern in FORBIDDEN)
    ]


def test_machinery_is_family_string_free() -> None:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in kit_files():
        rel = str(path.relative_to(KIT_ROOT))
        if rel in ENCLAVE or rel in PROVENANCE:
            continue
        if rel.startswith("tests/wiki-event/") and rel.endswith(".json"):
            # v1-shim fixtures; their own confinement test below asserts
            # each hit-carrying fixture is v1-shaped.
            continue
        found = hits(path)
        if found:
            offenders[rel] = found
    assert not offenders, (
        "family strings outside the documented enclave/provenance sets:\n"
        + "\n".join(
            f"  {rel}:{lineno}: {line}"
            for rel, lines in sorted(offenders.items())
            for lineno, line in lines
        )
    )


def test_enclave_files_confine_hits_to_the_shim() -> None:
    """Inside the enclave, every family-string line must be shim context:
    the two constants, the v1 envelope key, or a v1-marked test/fixture
    region. This stops the enclave from becoming a general exemption."""
    shim_markers = (
        "V1_ENVELOPE_KEY",
        "V1_REPO_NAME",
        '"aura"',
        "'aura'",
        "v1",
        "V1",
        "schema_version=1",
        '"schema_version": 1',
    )
    for rel in sorted(ENCLAVE - {"tests/sweep/test_zero_aura_strings.py"}):
        path = KIT_ROOT / rel
        assert path.exists(), (
            f"enclave file {rel} is gone; update ENCLAVE rather than "
            "silently skipping its confinement assertions"
        )
        stray = [
            (lineno, line)
            for lineno, line in hits(path)
            if not any(marker in line for marker in shim_markers)
        ]
        assert not stray, (
            f"{rel} has family strings outside shim context:\n"
            + "\n".join(f"  {lineno}: {line}" for lineno, line in stray)
        )


def test_v1_fixtures_are_the_only_marked_fixture_exemption() -> None:
    """Fixture JSONs may carry the v1 envelope only when they declare
    schema_version 1 (they exist to exercise the shim)."""
    fixture_dir = KIT_ROOT / "tests" / "wiki-event"
    for path in sorted(fixture_dir.glob("*.json")):
        found = hits(path)
        if not found:
            continue
        text = path.read_text(encoding="utf-8")
        assert '"schema_version": 1' in text or '"aura"' in text, (
            f"{path.name} carries family strings but is not a v1-shim fixture"
        )


def test_ledger_freeze_grep_matches_the_sweep_terms() -> None:
    """The acceptance wording names /Users/<user>, the source GitHub slug,
    and family strings; keep the sweep's term list in sync with the grep a
    reviewer would run by hand."""
    result = subprocess.run(
        [
            "grep",
            "-r",
            "-l",
            "-i",
            "-E",
            "--exclude-dir=__pycache__",
            "mezmo|mshearer",
            str(KIT_ROOT / "scripts"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", (
        f"hand-grep found machinery hits the sweep missed: {result.stdout}"
    )
