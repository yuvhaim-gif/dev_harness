#!/usr/bin/env python3
"""Open Knowledge Format (OKF) conformance for the harness info layer.

Every ``spec_docs`` entry in ``AGENTS.md`` is treated as an OKF concept document
(https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md):
a markdown file whose YAML frontmatter carries a non-empty ``type``. Reserved
filenames (``index.md``/``log.md``) follow OKF's own rules and are exempt from
the ``type`` gate. Contract concepts additionally MUST NOT declare a ``timestamp``
-- that field is edit-time volatile and would churn the pinned contract hash.

Enforcement is minimal on purpose (OKF v0.1 conformance, plus the harness's
contract-hash rule) so the corpus stays maximally interoperable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from ledger import LedgerError, load_ledger

RESERVED_INDEX = "index.md"
RESERVED_LOG = "log.md"
RESERVED_FILENAMES = frozenset({RESERVED_INDEX, RESERVED_LOG})

_FM_RE = re.compile(r"\A---[^\n]*\n(?P<fm>.*?)\n?---[^\n]*\n?(?P<body>.*)\Z", re.DOTALL)


@dataclass
class ParsedDoc:
    has_frontmatter: bool
    frontmatter: dict[str, Any] | None
    body: str
    error: str = ""


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def is_reserved(path: str) -> bool:
    return _basename(path) in RESERVED_FILENAMES


def parse_document(text: str) -> ParsedDoc:
    """Split a concept file into its YAML frontmatter mapping and markdown body."""
    match = _FM_RE.match(text)
    if not match:
        return ParsedDoc(has_frontmatter=False, frontmatter=None, body=text)
    raw_fm = match.group("fm")
    body = match.group("body")
    try:
        data: Any = yaml.safe_load(raw_fm) if raw_fm.strip() else {}
    except yaml.YAMLError as exc:
        return ParsedDoc(
            has_frontmatter=True,
            frontmatter=None,
            body=body,
            error=f"invalid YAML frontmatter: {exc}",
        )
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return ParsedDoc(
            has_frontmatter=True, frontmatter=None, body=body, error="frontmatter is not a mapping"
        )
    return ParsedDoc(has_frontmatter=True, frontmatter=data, body=body)


def validate_concept_text(text: str, *, path: str, is_contract: bool) -> list[str]:
    """Return OKF conformance problems for ``text`` at ``path`` (empty == OK)."""
    name = _basename(path)
    doc = parse_document(text)

    if name == RESERVED_LOG:
        return []

    if name == RESERVED_INDEX:
        if not doc.has_frontmatter:
            return []
        if doc.frontmatter is None:
            return [f"{path}: index.md has a malformed frontmatter block ({doc.error})."]
        extra = set(doc.frontmatter) - {"okf_version"}
        if extra:
            return [
                f"{path}: index.md frontmatter may only contain 'okf_version' "
                f"(OKF §6), found {sorted(extra)}."
            ]
        return []

    if not doc.has_frontmatter:
        return [f"{path}: missing OKF YAML frontmatter block (needs a non-empty 'type')."]
    if doc.frontmatter is None:
        return [f"{path}: malformed OKF frontmatter ({doc.error})."]

    problems: list[str] = []
    typ = doc.frontmatter.get("type")
    if not isinstance(typ, str) or not typ.strip():
        problems.append(f"{path}: OKF frontmatter must set a non-empty 'type'.")
    if is_contract and "timestamp" in doc.frontmatter:
        problems.append(
            f"{path}: contract concepts must not declare a volatile 'timestamp' "
            "(it would churn the pinned contract hash)."
        )
    return problems


def validate_concept(path: str, *, is_contract: bool) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return [f"{path}: declared spec_doc is missing on disk."]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: could not read spec_doc ({exc})."]
    return validate_concept_text(text, path=path, is_contract=is_contract)


def spec_map_from_ledger(ledger: Mapping[str, Any]) -> dict[str, bool]:
    """Map every declared spec_doc path -> whether it is a contract in any task."""
    result: dict[str, bool] = {}
    tasks = ledger.get("tasks") or {}
    if not isinstance(tasks, dict):
        return result
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        contracts = set(task.get("contracts") or [])
        for sd in task.get("spec_docs") or []:
            result[sd] = result.get(sd, False) or (sd in contracts)
    return result


def verify_paths(spec_map: Mapping[str, bool]) -> list[str]:
    problems: list[str] = []
    for path in sorted(spec_map):
        problems.extend(validate_concept(path, is_contract=spec_map[path]))
    return problems


def _load_ledger(ledger_path: str) -> dict[str, Any]:
    # Route through the canonical loader; an unusable ledger (missing, invalid
    # YAML, non-mapping) yields no spec_docs to validate here -- the dedicated
    # validate-agents-ledger hook is what fails loudly on a broken AGENTS.md.
    try:
        return load_ledger(ledger_path)
    except LedgerError:
        return {}


def verify(ledger_path: str = "AGENTS.md") -> list[str]:
    """Validate every spec_doc declared in ``ledger_path`` (empty == OK)."""
    ledger = _load_ledger(ledger_path)
    return verify_paths(spec_map_from_ledger(ledger))


def reserved_paths(paths: Iterable[str]) -> list[str]:
    return sorted(p for p in paths if is_reserved(p))
