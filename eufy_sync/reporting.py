"""Current-run facts used to describe sync results accurately."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class SyncReport:
    """Evidence collected during this invocation, including across retries."""

    garmin_existing_dates: set[tuple[str, date]] = field(default_factory=set)
    multiple_users: bool = False


def garmin_existing_note(report: SyncReport | None) -> str:
    """Describe Garmin same-date guard skips observed in this invocation."""
    if report is None or not report.garmin_existing_dates:
        return ""
    if report.multiple_users:
        count = len({user for user, _ in report.garmin_existing_dates})
        noun = "profile" if count == 1 else "profiles"
        return f" Garmin skipped existing weigh-ins for {count} {noun}."
    dates = {day for _, day in report.garmin_existing_dates}
    count = len(dates)
    if count == 1:
        return f" Garmin already has a weigh-in dated {next(iter(dates)).isoformat()}."
    if count > 1:
        return f" Garmin already has weigh-ins for {count} dates."
    return ""


def update_counts_summary(counts: dict[str, int], *, planned: bool = False) -> str:
    """Describe per-target updates while preserving insertion order."""
    label = "Syncs planned" if planned else "Syncs completed"
    parts = [f"{name.capitalize()} {count}" for name, count in counts.items()]
    return f"{label}: {', '.join(parts)}."
