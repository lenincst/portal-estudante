"""Contract validation and change detection for Ulife responses.

Each endpoint has an *expected contract*: the fields the client relies on and their JSON types.
`check()` compares a live response against it and:
- raises `SchemaChangedError` (breaking) when a required field disappears, becomes null or changes type,
  listing every issue with its JSON path and the client function to update;
- compares all observed field paths with a per-endpoint *baseline* (first sight is stored silently); later new
  or vanished fields are logged as non-breaking drift. Accepting new fields into the baseline is the only
  "safe automatic update"; removals/type changes of required fields are never auto-accepted.

Baseline and drift log live in the private data dir (contracts/) (field names only, no values).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import config
from .errors import ContractIssue, SchemaChangedError, PortalError

log = logging.getLogger(__name__)

JSON_TYPES = {
    "str": (str,),
    "int": (int,),
    "num": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


@dataclass(frozen=True)
class Field:
    """Expected field. `kind` is one of JSON_TYPES; `nullable` allows null; `required` = breaking if absent."""

    kind: str
    required: bool = True
    nullable: bool = False
    item: Shape | None = None  # for lists of objects / nested dicts


@dataclass(frozen=True)
class Shape:
    fields: dict[str, Field] = field(default_factory=dict)


@dataclass(frozen=True)
class Contract:
    endpoint: str  # human id, e.g. "GET apistudent/MyCourses/courses"
    root: Shape
    list_path: tuple[str, ...] = ()  # where the main list lives, for docs
    hint: str = ""  # what to update when it breaks (module/function)


def _walk(shape: Shape, data: Any, path: str, issues: list[ContractIssue]) -> None:
    """Breaking checks only: required fields present, with the expected JSON type."""
    if not isinstance(data, dict):
        issues.append(ContractIssue(path or "$", "object", _type_name(data)))
        return
    for name, spec in shape.fields.items():
        p = f"{path}.{name}" if path else f"$.{name}"
        if name not in data:
            if spec.required:
                issues.append(ContractIssue(p, f"field {spec.kind}", "missing"))
            continue
        value = data[name]
        if value is None:
            if not spec.nullable and spec.required:
                issues.append(ContractIssue(p, spec.kind, "null"))
            continue
        if not isinstance(value, JSON_TYPES[spec.kind]) or (spec.kind in ("int", "num") and isinstance(value, bool)):
            issues.append(ContractIssue(p, spec.kind, _type_name(value)))
            continue
        if spec.item is not None:
            if spec.kind == "list":
                for i, element in enumerate(value[:3]):  # a sample of elements is enough to detect breakage
                    _walk(spec.item, element, f"{p}[{i}]", issues)
            elif spec.kind == "dict":
                _walk(spec.item, value, p, issues)


def field_paths(data: Any, path: str = "$", depth: int = 6) -> set[str]:
    """All field paths of a JSON document, with list indexes normalized to [] (e.g. $.Result[].Name)."""
    out: set[str] = set()
    if depth <= 0:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            p = f"{path}.{k}"
            out.add(p)
            out |= field_paths(v, p, depth - 1)
    elif isinstance(data, list):
        if data:
            out.add(f"{path}[]")  # marks "list observed with elements", parent of its item fields
        for element in data[:5]:
            out |= field_paths(element, f"{path}[]", depth - 1)
    return out


class Baseline:
    """Per-endpoint set of field paths seen before. First sight = silent baseline; later differences = drift.

    Accepting new fields into the baseline after logging them is the only automatic update performed
    (safe: the client does not depend on them). Removed or retyped *required* fields are breaking errors.
    """

    def __init__(self) -> None:
        self.dir = config.DATA_DIR / "contracts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "baseline.json"
        self.log_path = self.dir / "drift.jsonl"
        try:
            self.data: dict[str, list[str]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def compare(self, endpoint: str, observed: set[str]) -> list[str]:
        known = set(self.data.get(endpoint, []))
        if not known:
            self.data[endpoint] = sorted(observed)
            self._save()
            return []
        added = sorted(observed - known)
        # A field only counts as removed when its parent object was observed (an empty list hides its children
        # legitimately and must not be reported as a change).
        removed = sorted(p for p in known - observed
                         if not p.endswith("[]") and (p.rsplit(".", 1)[0] in observed or p.rsplit(".", 1)[0] == "$"))
        messages = [f"new field: {p}" for p in added] + [f"field no longer present: {p}" for p in removed]
        if messages:
            with self.log_path.open("a", encoding="utf-8") as f:
                for m in messages:
                    f.write(json.dumps({"t": time.time(), "endpoint": endpoint, "message": m}, ensure_ascii=False) + "\n")
                    log.warning("Ulife drift (non-breaking) at %s: %s", endpoint, m)
            # Safe automatic update: accept new fields into the baseline; removed ones stay known and are
            # reported again on every run while they remain absent.
            self.data[endpoint] = sorted(known | observed)
            self._save()
        return messages

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=0), encoding="utf-8")


_baseline: Baseline | None = None


def check(contract: Contract, data: Any) -> Any:
    """Validate `data` against `contract`. Returns data unchanged; raises SchemaChangedError on breaking changes."""
    global _baseline
    issues: list[ContractIssue] = []
    _walk(contract.root, data, "", issues)
    if _baseline is None:
        _baseline = Baseline()
    _baseline.compare(contract.endpoint, field_paths(data))
    if issues:
        raise SchemaChangedError(contract.endpoint, issues, hint=contract.hint)
    return data


def check_envelope(endpoint: str, data: Any) -> Any:
    """Ulife's standard envelope `{Result, Success, ErrorMessage, ...}`: Success must be true."""
    if isinstance(data, dict) and "Success" in data and data.get("Success") is not True:
        raise PortalError(f"Ulife reported failure at {endpoint}: {data.get('ErrorMessage') or 'Success=false'} (ErrorType={data.get('ErrorType')})")
    return data
