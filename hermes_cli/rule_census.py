"""Spec 002 Mission Engine — rule census loader + coverage report.

Part of the Mission Engine rework GOVERNANCE-AS-DATA phase (P6, Jared-approved
program 2026-07-05).  The census file
``~/.hermes/specs/002-mission-engine/rules-census.yaml`` is the honest
inventory of every imperative MUST/BLOCK/refuse clause in the Spec 002
corpus, classed as:

* ``enforced`` — a production code path blocks the violation today,
* ``measured`` — a production code path records/surfaces it without blocking,
* ``advisory`` — prose only.

:func:`coverage` turns the census into the "governance proprioception" number
the engine-judge scorecard tracks: how much of the constitution is actually
load-bearing.  Run ``python -m hermes_cli.rule_census`` to print the census
table plus the coverage line (stdout is Discord-deliverable via the cron
layer per the rework conventions).

Env overrides: ``HERMES_RULES_CENSUS_PATH`` (file path),
``HERMES_KANBAN_HOME`` (shared root).  The loader fails closed
(:class:`MissionRuleCensusError`, a ``ValueError`` subclass) on a missing or
structurally invalid census — an unreadable inventory must not silently
report zero rules.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

_log = logging.getLogger(__name__)

CENSUS_PATH_ENV = "HERMES_RULES_CENSUS_PATH"
KANBAN_HOME_ENV = "HERMES_KANBAN_HOME"
CENSUS_RELPATH = ("specs", "002-mission-engine", "rules-census.yaml")

CENSUS_KIND = "mission_rules_census"
VALID_CLASSES = ("enforced", "measured", "advisory")


class MissionRuleCensusError(ValueError):
    """The rule census is missing, corrupt, or structurally invalid."""


@dataclass(frozen=True)
class CensusRule:
    rule_id: str
    source_file: str
    source_line: int
    clause: str
    rule_class: str
    enforcement_point: str

    @property
    def source(self) -> str:
        return f"{self.source_file}:{self.source_line}"


def census_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(CENSUS_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    root_override = (env.get(KANBAN_HOME_ENV) or "").strip()
    if root_override:
        return Path(root_override).expanduser().joinpath(*CENSUS_RELPATH)
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    except Exception:  # pragma: no cover - constants module always present in repo
        root = Path.home() / ".hermes"
    return Path(root).joinpath(*CENSUS_RELPATH)


def _require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        raise MissionRuleCensusError(f"invalid rule census at {path}: {message}")


def load_census(
    path: Optional[Path | str] = None, env: Optional[Mapping[str, str]] = None
) -> tuple:
    """Parse + validate the census. Returns a tuple of :class:`CensusRule`.

    Validates: kind, non-empty rules list, unique rule_ids, valid classes,
    parseable ``source_file:line`` references.
    """
    resolved = Path(path).expanduser() if path is not None else census_path(env)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissionRuleCensusError(f"rule census not readable at {resolved}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MissionRuleCensusError(f"corrupt rule census YAML at {resolved}: {exc}") from exc

    _require(isinstance(data, dict), resolved, "top-level document must be a mapping")
    kind = str(data.get("kind", "")).strip()
    _require(kind == CENSUS_KIND, resolved, f"kind is {kind!r}, expected {CENSUS_KIND!r}")
    raw_rules = data.get("rules")
    _require(isinstance(raw_rules, list) and raw_rules, resolved, "rules must be a non-empty list")

    rules = []
    seen: set = set()
    for entry in raw_rules:
        _require(isinstance(entry, dict), resolved, "each rule must be a mapping")
        rule_id = str(entry.get("rule_id", "")).strip()
        _require(bool(rule_id), resolved, "rule_id is required")
        _require(rule_id not in seen, resolved, f"duplicate rule_id {rule_id}")
        seen.add(rule_id)

        source = str(entry.get("source", "")).strip()
        _require(":" in source, resolved, f"{rule_id}: source must be file:line, got {source!r}")
        source_file, _, line_text = source.rpartition(":")
        try:
            source_line = int(line_text)
        except ValueError:
            raise MissionRuleCensusError(
                f"invalid rule census at {resolved}: {rule_id}: source line "
                f"{line_text!r} is not an integer"
            ) from None
        _require(source_line > 0, resolved, f"{rule_id}: source line must be positive")

        clause = str(entry.get("clause", "")).strip()
        _require(bool(clause), resolved, f"{rule_id}: clause is required")

        rule_class = str(entry.get("class", "")).strip().lower()
        _require(
            rule_class in VALID_CLASSES,
            resolved,
            f"{rule_id}: class {rule_class!r} not in {VALID_CLASSES}",
        )

        point = str(entry.get("enforcement_point", "")).strip() or "—"
        _require(
            rule_class == "advisory" or (point and point != "—"),
            resolved,
            f"{rule_id}: class {rule_class!r} requires a concrete enforcement_point",
        )

        rules.append(
            CensusRule(
                rule_id=rule_id,
                source_file=source_file,
                source_line=source_line,
                clause=clause,
                rule_class=rule_class,
                enforcement_point=point,
            )
        )
    return tuple(rules)


def coverage(
    rules: Optional[tuple] = None,
    *,
    path: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Counts by class + % enforced/measured/advisory for the census."""
    if rules is None:
        rules = load_census(path=path, env=env)
    counts = Counter(rule.rule_class for rule in rules)
    total = len(rules)
    result: dict = {"total": total}
    for cls in VALID_CLASSES:
        n = counts.get(cls, 0)
        result[cls] = n
        result[f"pct_{cls}"] = round(100.0 * n / total, 1) if total else 0.0
    return result


def _main() -> int:
    try:
        rules = load_census()
    except MissionRuleCensusError as exc:
        print(f"RULE CENSUS ERROR: {exc}")
        return 2

    id_w = max(len(r.rule_id) for r in rules)
    src_w = max(len(r.source) for r in rules)
    cls_w = max(len(r.rule_class) for r in rules)
    print(f"{'RULE':<{id_w}}  {'CLASS':<{cls_w}}  {'SOURCE':<{src_w}}  CLAUSE / ENFORCEMENT")
    print("-" * (id_w + cls_w + src_w + 60))
    for r in rules:
        clause = r.clause if len(r.clause) <= 76 else r.clause[:73] + "..."
        print(f"{r.rule_id:<{id_w}}  {r.rule_class:<{cls_w}}  {r.source:<{src_w}}  {clause}")
        if r.enforcement_point != "—":
            print(f"{'':<{id_w}}  {'':<{cls_w}}  {'':<{src_w}}    -> {r.enforcement_point}")
    cov = coverage(rules)
    print("-" * (id_w + cls_w + src_w + 60))
    print(
        f"COVERAGE: {cov['total']} rules — "
        f"{cov['enforced']} enforced ({cov['pct_enforced']}%) / "
        f"{cov['measured']} measured ({cov['pct_measured']}%) / "
        f"{cov['advisory']} advisory ({cov['pct_advisory']}%)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    raise SystemExit(_main())
