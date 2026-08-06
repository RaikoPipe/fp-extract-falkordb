"""tqdm-style time estimator for live progress UIs.

A small, dependency-free helper that mirrors the parts of ``tqdm`` we need
for the Chainlit ingestion panel: track completion count against a total,
measure elapsed wall time, and expose a smoothed iteration rate so we can
compute a remaining-time estimate (ETA). The estimator is pure-Python and
synchronous; the Chainlit progress layer (``chainlit_progress.py``) calls
into it from its async event handler.

The smoothing follows tqdm's default EMA window: the instantaneous rate is
``1 / max(dt, 1e-6)`` per item, smoothed by an exponential moving average
with ``alpha = 1 / smooth_window`` (``smooth_window = 10`` by default). This
keeps the ETA stable across a few slow chunks while still converging when
the rate shifts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

_SMOOTH_WINDOW = 10
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
    _last_time: float = field(init=False)
    _ema_rate: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._last_time = self.start_time

    def reset(self, total: int | None = None) -> None:
        """Reset the estimator, optionally changing the total.

        Keeps the same instance so callers holding a reference see the
        new state. ``total=None`` reuses the existing total.
        """
        if total is not None:
            self.total = total
        self.n = 0
        self.start_time = time.monotonic()
        self._last_time = self.start_time
        self._ema_rate = 0.0

    def update(self, count: int = 1) -> None:
        """Record ``count`` newly-completed items and refresh the rate EMA.

        The EMA update is computed once per :meth:`update` call (not per
        item), so a batched ``update(5)`` advances the EMA by one tick.
        Multiple ticks within ``_MIN_DT`` seconds are clamped to avoid
        division-by-zero spikes in the instantaneous rate.
        """
        self.n += count
        if self.n < 0:
            self.n = 0
        now = time.monotonic()
        dt = max(now - self._last_time, _MIN_DT)
        instant = count / dt
        if self._ema_rate <= 0:
            self._ema_rate = instant
        else:
            alpha = 1.0 / _SMOOTH_WINDOW
            self._ema_rate = (1 - alpha) * self._ema_rate + alpha * instant
        self._last_time = now

    @property
    def elapsed(self) -> float:
        """Wall-clock seconds since :meth:`reset` / construction."""
        return max(time.monotonic() - self.start_time, 0.0)

    @property
    def rate(self) -> float:
        """Smoothed items-per-second rate (EMA over ``_SMOOTH_WINDOW``)."""
        return self._ema_rate

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
        ``remaining / rate`` using the smoothed rate, which is more stable
        than the raw instantaneous rate (especially right after the first
        chunk when a single slow LLM call would otherwise blow up the
        estimate).
        """
        if self.total <= 0 or self._ema_rate <= 0:
            return -1.0
        if self.n >= self.total:
            return 0.0
        return self.remaining / self._ema_rate

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