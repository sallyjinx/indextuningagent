"""Current condition names and read-only compatibility for historical logs."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONDITIONS = ("no_tuning", "rule", "autonomous")
CONDITION_LABELS = {
    "no_tuning": "No tuning",
    "rule": "Rule baseline",
    "autonomous": "Autonomous tuning",
}

# Persisted legacy identifiers belong to compatibility data, not active names.
CONDITION_ALIASES = json.loads((ROOT / "compatibility/condition-aliases.json").read_text(encoding="utf-8"))
if not isinstance(CONDITION_ALIASES, dict) or any(
    not isinstance(alias, str) or target not in CONDITIONS or alias in CONDITIONS
    for alias, target in CONDITION_ALIASES.items()
):
    raise ValueError("Invalid historical condition aliases")


def canonical_condition(value):
    """Translate only documented aliases; retain unknown labels for auditing."""
    return CONDITION_ALIASES.get(value, value) if isinstance(value, str) else value


def normalize_events(events: list[dict]) -> list[dict]:
    """Copy event dictionaries and normalize only top-level condition fields.

    Nested model output, measurements and frozen evidence remain untouched.
    The original dictionaries and the on-disk journal are never modified.
    """
    return [
        {**event, "condition": canonical_condition(event["condition"])}
        if "condition" in event else dict(event)
        for event in events
    ]
