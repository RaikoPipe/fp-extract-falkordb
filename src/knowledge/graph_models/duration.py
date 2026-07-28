"""Duration-string schema for time-valued fields.

Factory-planning documents describe durations either as a constant
(e.g. "40 s") or as a probability distribution (e.g. "normally distributed
with mean 300 s, std 45 s"). To keep the extraction schema uniform, every
time-valued field on the domain models is a **string** that follows a fixed
grammar:

Constant
    ``d=<number>s``  ->  e.g. ``d=40s``

Distribution
    ``<name>(<key>=<number>, <key>=<number>)``  ->  e.g.
    ``normal(mean=300, std=45)`` or ``uniform(min=10, max=20)``

All values are **seconds** (the trailing ``s`` on a constant is mandatory;
distributions are implicitly in seconds). Only the canonical keys
``mean`` and ``std`` are used for normal/lognormal distributions; ``min`` and
``max`` for uniform/triangular; ``lambda`` for exponential; ``k`` and
``lambda`` for erlang/weibull. Any other spelling (``mu``, ``sigma``, ...) is
silently rewritten to the canonical form during validation.

If a value cannot be parsed at all, it is kept verbatim and recorded as an
**ambiguous duration** so the caller (the extraction pipeline) can surface it
in the ``FactoryPlanningGraph.ambiguous_durations`` list for human review.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

# Canonical distribution names.
_DISTRIBUTION_NAMES = {
    "normal",
    "exponential",
    "uniform",
    "triangular",
    "weibull",
    "lognormal",
    "erlang",
    "empirical",
}

# Aliases that are silently rewritten to canonical keys.
_KEY_ALIASES = {
    "mu": "mean",
    "sigma": "std",
    "stddev": "std",
    "standarddeviation": "std",
    "mean_value": "mean",
    "rate": "lambda",
    "scale": "lambda",
    "shape": "k",
}

# --- regexes ---------------------------------------------------------------
# constant: d=40s  (optional spaces, integer or decimal, mandatory trailing s)
_CONSTANT_RE = re.compile(r"^d\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*s\s*$")

# distribution: name(arg=val, arg=val)  - tolerant about whitespace
_DISTRIBUTION_RE = re.compile(
    r"^(?P<name>[a-zA-Z]+)\s*\((?P<args>.*)\)\s*$"
)
_ARG_RE = re.compile(r"\s*([a-zA-Z_]+)\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*")


@dataclass
class AmbiguousRecord:
    """One field whose duration string could not be parsed."""

    entity_type: str
    entity_name: str
    field_name: str
    raw_value: str


@dataclass
class ParseResult:
    """Outcome of parsing a duration string."""

    canonical: str
    is_ambiguous: bool
    kind: str  # "constant" | "distribution" | "ambiguous"
    constant_seconds: Optional[float] = None
    distribution_name: Optional[str] = None
    distribution_params: dict[str, float] = field(default_factory=dict)


# ContextVar: validators record ambiguous entries here so the extraction
# pipeline can harvest them after a model_validate_* call.
_ambiguous_sink: ContextVar[list[AmbiguousRecord]] = ContextVar(
    "_ambiguous_sink", default=None
)


def reset_ambiguous_sink() -> list[AmbiguousRecord]:
    """Install a fresh sink and return it. Call before model validation."""
    sink: list[AmbiguousRecord] = []
    _ambiguous_sink.set(sink)
    return sink


def get_ambiguous_records() -> list[AmbiguousRecord]:
    """Return the current sink (empty list if none installed)."""
    sink = _ambiguous_sink.get()
    return sink if sink is not None else []


def _canonicalize_distribution(name: str, params: dict[str, float]) -> Optional[str]:
    """Return canonical ``name(key=val, key=val)`` string, or None if invalid."""
    cname = name.lower().strip()
    if cname not in _DISTRIBUTION_NAMES:
        return None
    if not params:
        return None
    parts = [f"{k}={_fmt(v)}" for k, v in params.items()]
    return f"{cname}({', '.join(parts)})"


def _fmt(v: float) -> str:
    """Format a number without trailing .0 when integral."""
    if v == int(v):
        return str(int(v))
    return repr(v)


def _canonicalize_constant(seconds: float) -> str:
    return f"d={_fmt(seconds)}s"


def parse_duration(value: str) -> ParseResult:
    """Parse a duration string into a :class:`ParseResult`.

    Never raises: unparseable input is returned as ``kind="ambiguous"`` with
    the raw value preserved in ``canonical``.
    """
    if value is None:
        return ParseResult(canonical="", is_ambiguous=True, kind="ambiguous")
    raw = value.strip()
    if not raw:
        return ParseResult(canonical="", is_ambiguous=True, kind="ambiguous")

    # constant
    m = _CONSTANT_RE.match(raw)
    if m:
        secs = float(m.group(1))
        return ParseResult(
            canonical=_canonicalize_constant(secs),
            is_ambiguous=False,
            kind="constant",
            constant_seconds=secs,
        )

    # distribution
    m = _DISTRIBUTION_RE.match(raw)
    if m:
        name = m.group("name")
        arg_str = m.group("args")
        params: dict[str, float] = {}
        ok = True
        for piece in arg_str.split(","):
            piece = piece.strip()
            if not piece:
                continue
            am = _ARG_RE.match(piece)
            if not am:
                ok = False
                break
            key = am.group(1).lower()
            val = float(am.group(2))
            key = _KEY_ALIASES.get(key, key)
            params[key] = val
        if ok and params:
            canon = _canonicalize_distribution(name, params)
            if canon is not None:
                return ParseResult(
                    canonical=canon,
                    is_ambiguous=False,
                    kind="distribution",
                    distribution_name=name.lower(),
                    distribution_params=params,
                )

    return ParseResult(canonical=raw, is_ambiguous=True, kind="ambiguous")


def record_ambiguous(entity_type: str, entity_name: str, field_name: str, raw_value: str) -> None:
    """Append an ambiguous-duration record to the current sink (if any)."""
    sink = _ambiguous_sink.get()
    if sink is None:
        return
    sink.append(
        AmbiguousRecord(
            entity_type=entity_type,
            entity_name=entity_name,
            field_name=field_name,
            raw_value=raw_value,
        )
    )


def validate_duration(value: Optional[str], entity_type: str, entity_name: str, field_name: str) -> Optional[str]:
    """Pydantic-friendly validator.

    Returns the canonicalized string, or None for a null input. Records
    ambiguous values to the current sink.
    """
    if value is None or value == "":
        return None
    result = parse_duration(value)
    if result.is_ambiguous:
        record_ambiguous(entity_type, entity_name, field_name, value)
    return result.canonical