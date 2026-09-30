"""Small pure helpers shared by the platforms (no Home Assistant imports)."""

from __future__ import annotations

import math
from typing import Any


def _decimals(coeff: float) -> int:
    """Number of decimals needed to represent values scaled by coeff."""
    if coeff == 0:
        return 0
    return max(0, math.ceil(round(-math.log10(abs(coeff)), 9)))


def scale_value(raw: Any, coeff: float | None) -> Any:
    """Scale a raw register value, avoiding float noise like 28.700000000000003."""
    if coeff is None or coeff == 1:
        return raw
    return round(raw * coeff, _decimals(coeff))


def enum_option(options: list[str], index: Any) -> str | None:
    """Return options[index], or None if the index is out of range / unnamed.

    Guards against negative int16 values silently wrapping to the end of the
    list and against unknown values raising IndexError inside an entity.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if 0 <= index < len(options):
        return options[index] or None
    return None


def to_int16(value: int) -> int:
    """Interpret an unsigned 16 bit register value as signed."""
    return value - 0x10000 if value & 0x8000 else value
