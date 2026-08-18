from dataclasses import dataclass, field


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
