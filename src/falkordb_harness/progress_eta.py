"""tqdm-style time estimator for live progress UIs.

A small, dependency-free helper that mirrors the parts of ``tqdm`` we need
for the Chainlit ingestion panel: track completion count against a total,
measure elapsed wall time, and expose an iteration rate so we can compute
a remaining-time estimate (ETA). The estimator is pure-Python and
synchronous; the Chainlit progress layer (``chainlit_progress.py``) calls
into it from its async event handler.

The rate is the cumulative average ``n / elapsed`` rather than a per-tick
EMA. The ingestion pipeline extracts chunks with bounded concurrency
(``extract_from_chunks`` runs N LLM calls in flight), so completion ticks
arrive in clusters: several chunks finish within microseconds of each
other, then a long pause until the next batch. A per-tick EMA (tqdm's
``1 / dt`` formula, designed for serial iteration) saturates at the
``_MIN_DT`` clamp during each cluster and produces rates in the millions of
it/s, which collapses the ETA to 0:00. The cumulative average is immune to
clustering because both ``n`` and ``elapsed`` are monotone and unaffected
by the inter-tick gap distribution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

_MIN_DT = 1e-6


def format_duration(seconds: float) -> str:
    """Format a duration (seconds) into a compact ``tqdm``-style string.

    Examples:
        ``0.5   -> "0:00"``
        ``65    -> "1:05"``
        ``3700  -> "1:01:40"``
        ``-1    -> "?"``       (unknown / not yet estimable)
    """
    if seconds is None or seconds < 0 or not _is_finite(seconds):
        return "?"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def _is_finite(x: float) -> bool:
    try:
        return not (x != x or x in (float("inf"), float("-inf")))
    except TypeError:
        return False


@dataclass
class TimeEstimator:
    """Track (completed / total) and report tqdm-style timing fields.

    Call :meth:`update` each time an item finishes; read
    :attr:`elapsed`, :attr:`rate`, :attr:`eta`, and :attr:`percent` for the
    current display values. The estimator is monotonic in ``n`` — calling
    :meth:`update` with ``n`` past the total is allowed and clamps the
    percent to 100%.

    Attributes:
        total: The total number of items (must be > 0 for ETA).
        n: Completed items so far.
        start_time: ``time.monotonic()`` at construction or :meth:`reset`.
    """

    total: int = 0
    n: int = 0
    start_time: float = field(default_factory=time.monotonic)

    def reset(self, total: int | None = None) -> None:
        """Reset the estimator, optionally changing the total.

        Keeps the same instance so callers holding a reference see the
        new state. ``total=None`` reuses the existing total.
        """
        if total is not None:
            self.total = total
        self.n = 0
        self.start_time = time.monotonic()

    def update(self, count: int = 1) -> None:
        """Record ``count`` newly-completed items.

        The rate is derived from cumulative ``n`` and :attr:`elapsed`, so
        this method only advances the counter (no per-tick rate state).
        Batched ``update(5)`` is equivalent to five ``update(1)`` calls.
        """
        self.n += count
        if self.n < 0:
            self.n = 0

    @property
    def elapsed(self) -> float:
        """Wall-clock seconds since :meth:`reset` / construction."""
        return max(time.monotonic() - self.start_time, 0.0)

    @property
    def rate(self) -> float:
        """Cumulative-average items-per-second rate (``n / elapsed``).

        Returns ``0.0`` before the first tick (or if fewer than
        ``_MIN_DT`` seconds have elapsed), which the :attr:`eta` property
        treats as the "not yet estimable" sentinel.
        """
        e = self.elapsed
        if e <= _MIN_DT:
            return 0.0
        return self.n / e

    @property
    def remaining(self) -> int:
        """Items left to complete (clamped at 0)."""
        if self.total <= 0:
            return 0
        return max(self.total - self.n, 0)

    @property
    def percent(self) -> float:
        """Completion percentage in ``[0.0, 100.0]``."""
        if self.total <= 0:
            return 0.0
        return min(self.n / self.total, 1.0) * 100.0

    @property
    def eta(self) -> float:
        """Estimated remaining seconds (``-1.0`` if not yet estimable).

        Returns ``-1.0`` when no items have completed or the total is
        unknown / zero, matching tqdm's "unknown" sentinel. The ETA is
        ``remaining / rate`` using the cumulative-average rate, which is
        stable under the clustered-completion pattern of concurrent
        extraction (see module docstring).
        """
        if self.total <= 0:
            return -1.0
        if self.n >= self.total:
            return 0.0
        r = self.rate
        if r <= 0:
            return -1.0
        return self.remaining / r

    def render(self) -> dict[str, str | float]:
        """Return a dict of display-ready fields for the UI layer.

        Keys: ``n``, ``total``, ``percent``, ``elapsed``, ``eta``,
        ``rate``, ``remaining``. String values are pre-formatted
        (``elapsed``/``eta`` via :func:`format_duration`); numeric values
        are raw floats for callers that want to format themselves.
        """
        return {
            "n": self.n,
            "total": self.total,
            "percent": self.percent,
            "elapsed": self.elapsed,
            "elapsed_str": format_duration(self.elapsed),
            "eta": self.eta,
            "eta_str": format_duration(self.eta),
            "rate": self.rate,
            "remaining": self.remaining,
        }


__all__ = ["TimeEstimator", "format_duration"]