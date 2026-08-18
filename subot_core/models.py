from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    """Durable states used by the SQLite listing queue."""

    BASELINE = "baseline"
    QUEUED = "queued"
    CLAIMED = "claimed"
    NOTIFIED = "notified"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.BASELINE,
        JobStatus.NOTIFIED,
        JobStatus.REJECTED,
        JobStatus.FAILED,
    }
)


@dataclass(frozen=True)
class ListingJob:
    """A listing payload and its durable queue metadata."""

    listing_id: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    last_error: str | None = None
    claimed_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None


@dataclass
class CycleStats:
    fetched: int = 0
    baselined: int = 0
    matched: int = 0
    notified: int = 0
    failures: int = 0


@dataclass
class SeenState:
    listing_ids: set = field(default_factory=set)
    initialized_searches: set = field(default_factory=set)
