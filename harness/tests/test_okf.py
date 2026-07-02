"""OKF info-layer conformance tests.

Cover the parser, the concept/reserved-file validation rules, the contract
``timestamp`` prohibition, and that the shipped example bundle plus the live
ledger are conformant end to end.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "harness"))

import okf  # noqa: E402

VALID_CONCEPT = "---\ntype: Reference\ntitle: X\n---\n\nbody\n"


def test_parses_frontmatter_and_body() -> None:
    doc = okf.parse_document(VALID_CONCEPT)
    assert doc.has_frontmatter is True
    assert doc.frontmatter == {"type": "Reference", "title": "X"}
    assert "body" in doc.body


def test_empty_frontmatter_block_parses_as_empty_mapping() -> None:
    doc = okf.parse_document("---\n---\nbody\n")
    assert doc.has_frontmatter is True
    assert doc.frontmatter == {}


def test_concept_requires_frontmatter() -> None:
    problems = okf.validate_concept_text("# just markdown\n", path="docs/a.md", is_contract=False)
    assert problems and "missing OKF" in problems[0]


def test_concept_requires_non_empty_type() -> None:
    problems = okf.validate_concept_text(
        "---\ntitle: X\n---\nbody\n", path="docs/a.md", is_contract=False
    )
    assert problems and "non-empty 'type'" in problems[0]


def test_valid_concept_passes() -> None:
    assert okf.validate_concept_text(VALID_CONCEPT, path="docs/a.md", is_contract=False) == []


def test_malformed_yaml_frontmatter_is_reported() -> None:
    bad = "---\ntype: [unclosed\n---\nbody\n"
    problems = okf.validate_concept_text(bad, path="docs/a.md", is_contract=False)
    assert problems and "malformed OKF frontmatter" in problems[0]


def test_contract_forbids_timestamp() -> None:
    text = "---\ntype: API Contract\ntimestamp: 2026-01-01T00:00:00Z\n---\nbody\n"
    problems = okf.validate_concept_text(text, path="docs/api.md", is_contract=True)
    assert problems and "timestamp" in problems[0]


def test_non_contract_allows_timestamp() -> None:
    text = "---\ntype: Notes\ntimestamp: 2026-01-01T00:00:00Z\n---\nbody\n"
    assert okf.validate_concept_text(text, path="docs/notes.md", is_contract=False) == []


def test_reserved_log_is_exempt() -> None:
    text = "# Log\n\n## 2026-01-01\n* x\n"
    assert okf.validate_concept_text(text, path="d/log.md", is_contract=False) == []


def test_reserved_index_allows_only_okf_version() -> None:
    ok_index = '---\nokf_version: "0.1"\n---\n# Index\n'
    assert okf.validate_concept_text(ok_index, path="d/index.md", is_contract=False) == []
    bad_index = "---\ntype: Reference\n---\n# Index\n"
    problems = okf.validate_concept_text(bad_index, path="d/index.md", is_contract=False)
    assert problems and "okf_version" in problems[0]


def test_index_without_frontmatter_passes() -> None:
    text = "# Index\n\n* [a](a.md)\n"
    assert okf.validate_concept_text(text, path="d/index.md", is_contract=False) == []


def test_is_reserved() -> None:
    assert okf.is_reserved("a/b/index.md") is True
    assert okf.is_reserved("a/b/log.md") is True
    assert okf.is_reserved("a/b/concept.md") is False


def test_spec_map_flags_contracts() -> None:
    ledger = {
        "tasks": {
            "t": {
                "spec_docs": ["docs/a.md", "docs/b.md"],
                "contracts": ["docs/b.md"],
            }
        }
    }
    assert okf.spec_map_from_ledger(ledger) == {"docs/a.md": False, "docs/b.md": True}


def test_missing_spec_doc_reported() -> None:
    problems = okf.verify_paths({"docs/does-not-exist.md": False})
    assert problems and "missing on disk" in problems[0]


def test_example_bundle_and_live_ledger_conform() -> None:
    ledger = os.path.join(REPO_ROOT, "AGENTS.md")
    problems = okf.verify(ledger_path=ledger)
    assert problems == [], "OKF non-conformance:\n" + "\n".join(problems)
