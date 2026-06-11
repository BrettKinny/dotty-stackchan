"""Household registry — YAML-backed source of truth for "who lives here."

Re-exports the same surface as bridge/household.py so consumers,
greeters, and the dashboard see an identical API across the cutover.
"""

from .registry import (
    DEFAULT_HOUSEHOLD_PATH,
    DEFAULT_PERSON_FALLBACK,
    HouseholdRegistry,
    Person,
)
from .resolver import PersonResolver

__all__ = [
    "DEFAULT_HOUSEHOLD_PATH",
    "DEFAULT_PERSON_FALLBACK",
    "HouseholdRegistry",
    "Person",
    "PersonResolver",
]
