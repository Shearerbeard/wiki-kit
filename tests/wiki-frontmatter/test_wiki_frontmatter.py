#!/usr/bin/env python3
"""Tests for the shared workstream frontmatter module."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from wiki_frontmatter import (  # noqa: E402
    FrontmatterError,
    format_frontmatter,
    parse_frontmatter,
    parse_workstream_file,
    validate_frontmatter,
    validate_workstream_body,
)

VALID_TEXT = (
    "---\n"
    "status: active\n"
    "branch: alex/some-branch\n"
    "sha: 4c0f549\n"
    "last_updated: 2026-06-12\n"
    'blocker: ""\n'
    "---\n"
    "\n"
    "## Heading\n"
    "\n"
    "Body text.\n"
)


class ParseFormatTest(unittest.TestCase):
    def test_parse_extracts_fields_and_body(self) -> None:
        fm, body = parse_frontmatter(VALID_TEXT)
        self.assertEqual(fm["status"], "active")
        self.assertEqual(fm["blocker"], "")
        self.assertTrue(body.startswith("\n## Heading"))

    def test_round_trip_is_identity(self) -> None:
        fm, body = parse_frontmatter(VALID_TEXT)
        self.assertEqual(format_frontmatter(fm) + body, VALID_TEXT)

    def test_round_trip_does_not_accumulate_newlines(self) -> None:
        # Regression: the pre-consolidation wiki_garden parse/format pair
        # added one blank line after the frontmatter per cycle; repeated
        # garden applies grew every workstream file.
        text = VALID_TEXT
        for _ in range(3):
            fm, body = parse_frontmatter(text)
            text = format_frontmatter(fm) + body
        self.assertEqual(text, VALID_TEXT)

    def test_quoting_contract(self) -> None:
        fm = {
            "status": "active",
            "blocker": "",
            "note": "has spaces here",
            "branch": "no-spaces",
        }
        formatted = format_frontmatter(fm)
        self.assertIn('blocker: ""\n', formatted)
        self.assertIn('note: "has spaces here"\n', formatted)
        self.assertIn("branch: no-spaces\n", formatted)
        # And the quoting survives a parse.
        parsed, _ = parse_frontmatter(formatted)
        self.assertEqual(parsed, fm)

    def test_key_order_preserved(self) -> None:
        fm, body = parse_frontmatter(VALID_TEXT)
        keys = [
            line.partition(":")[0] for line in format_frontmatter(fm).splitlines()[1:-1]
        ]
        self.assertEqual(keys, list(fm))

    def test_unrepresentable_value_refused(self) -> None:
        # The quote-strip parse cannot round-trip embedded double quotes or
        # newlines; the writer fails loud instead of writing lossily.
        for bad in ('say "hi"', "two\nlines"):
            with self.subTest(value=bad), self.assertRaises(FrontmatterError):
                format_frontmatter({"blocker": bad})

    def test_missing_open_delimiter_fails(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_frontmatter("## Just a heading\n")

    def test_missing_close_delimiter_fails(self) -> None:
        with self.assertRaises(FrontmatterError):
            parse_frontmatter("---\nstatus: active\n")

    def test_parse_workstream_file_names_the_file(self) -> None:
        # Rewritten from the source repo's real-file probe (its
        # _archive/index.md): the kit ships no content tree, so the
        # frontmatter-less file is synthetic.
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "no-frontmatter.md"
            bad.write_text("## Just a heading\n")
            with self.assertRaises(FrontmatterError) as ctx:
                parse_workstream_file(bad)
            self.assertIn("no-frontmatter.md", str(ctx.exception))


class ValidateTest(unittest.TestCase):
    def valid_fm(self) -> dict[str, str]:
        return {
            "status": "active",
            "branch": "main",
            "sha": "4c0f549",
            "last_updated": "2026-06-12",
        }

    def test_valid_frontmatter_passes(self) -> None:
        self.assertEqual(validate_frontmatter(self.valid_fm(), "x.md"), [])

    def test_missing_required_field(self) -> None:
        fm = self.valid_fm()
        del fm["sha"]
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("sha", errors[0])

    def test_empty_required_field(self) -> None:
        fm = self.valid_fm()
        fm["branch"] = ""
        self.assertEqual(len(validate_frontmatter(fm, "x.md")), 1)

    def test_invalid_status(self) -> None:
        fm = self.valid_fm()
        fm["status"] = "paused"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("paused", errors[0])

    def test_invalid_date(self) -> None:
        fm = self.valid_fm()
        fm["last_updated"] = "June 12"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("last_updated", errors[0])

    def test_unknown_template_fails(self) -> None:
        fm = self.valid_fm()
        fm["template"] = "future-v9"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("unsupported template", errors[0])

    def test_valid_epic_tier_pair_passes(self) -> None:
        fm = self.valid_fm()
        fm["epic"] = "widget-platform"
        fm["tier"] = "satellite"
        self.assertEqual(validate_frontmatter(fm, "x.md"), [])

    def test_invalid_tier_value(self) -> None:
        fm = self.valid_fm()
        fm["epic"] = "widget-platform"
        fm["tier"] = "child"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("invalid tier", errors[0])

    def test_tier_without_epic_fails(self) -> None:
        fm = self.valid_fm()
        fm["tier"] = "board-page"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires an 'epic'", errors[0])

    def test_epic_without_tier_fails(self) -> None:
        fm = self.valid_fm()
        fm["epic"] = "widget-platform"
        errors = validate_frontmatter(fm, "x.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("requires a 'tier'", errors[0])

    def test_old_body_shape_only_requires_session_updates(self) -> None:
        body = "## Notes\n\n## Session updates (uncurated)\n"
        self.assertEqual(validate_workstream_body(self.valid_fm(), body, "x.md"), [])

    def test_prose_v1_requires_named_sections(self) -> None:
        fm = self.valid_fm()
        fm["template"] = "prose-v1"
        body = "## Title\n\n### Current State\n\n## Session updates (uncurated)\n"
        errors = validate_workstream_body(fm, body, "x.md")
        self.assertTrue(any("What Was Done" in error for error in errors))
        self.assertTrue(any("Curated State" in error for error in errors))


CANONICAL_BODY = (
    "\n"
    "## Notes\n"
    "\n"
    "Fixture body text.\n"
    "\n"
    "## Session updates (uncurated)\n"
)


class FixtureTreeTest(unittest.TestCase):
    """Rewritten from the source repo's RealFilesTest: that class swept the
    deployment's real workstreams/ (top level plus _archive/) for parse,
    validation, and byte-canonical frontmatter. The kit ships no content,
    so the sweep runs on a synthetic corpus with the same shape — an
    active page and an archived page — asserting the same three
    properties. The real-file sweep stays with each deployment (the
    validate-workstreams CLI covers it there)."""

    def write_corpus(self, workstreams_dir: Path) -> list[Path]:
        (workstreams_dir / "_archive").mkdir(parents=True)
        files = [
            workstreams_dir / "widget-platform.md",
            workstreams_dir / "_archive" / "widget-launch.md",
        ]
        for path in files:
            path.write_text(
                format_frontmatter(
                    {
                        "status": "archived" if "_archive" in path.parts else "active",
                        "branch": "alex/widget-work",
                        "sha": "4c0f549",
                        "last_updated": "2026-06-12",
                        "blocker": "",
                    }
                )
                + CANONICAL_BODY
            )
        return files

    def test_corpus_parses_and_validates(self) -> None:
        with TemporaryDirectory() as tmp:
            for path in self.write_corpus(Path(tmp)):
                with self.subTest(file=path.name):
                    fm, body = parse_workstream_file(path)
                    self.assertEqual(validate_frontmatter(fm, path.name), [])
                    self.assertEqual(
                        validate_workstream_body(fm, body, path.name), []
                    )

    def test_corpus_frontmatter_is_canonical(self) -> None:
        # The writer contract holds on disk: re-formatting the parsed
        # frontmatter reproduces the file byte-for-byte. Catches hand edits
        # that drift from the single written form.
        with TemporaryDirectory() as tmp:
            for path in self.write_corpus(Path(tmp)):
                with self.subTest(file=path.name):
                    text = path.read_text()
                    fm, body = parse_frontmatter(text)
                    self.assertEqual(format_frontmatter(fm) + body, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
