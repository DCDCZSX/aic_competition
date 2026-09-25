"""Shared environment configuration parsing and validation."""

from __future__ import annotations

import os
from typing import Optional


class ConfigurationError(ValueError):
    """Raised when an environment value cannot satisfy its declared contract."""


def read_int_env(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Read one integer environment variable and enforce its bounds.

    An unset or whitespace-only value uses ``default`` so optional settings keep
    their documented defaults.  A non-empty invalid or out-of-range value is a
    startup configuration error instead of being silently clamped.
    """
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum cannot be greater than maximum")

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer") from exc

    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}")
    return value
