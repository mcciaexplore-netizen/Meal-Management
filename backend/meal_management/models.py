from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class ServerContext:
    session_token: str = field(repr=False)
    idle_timeout_seconds: int = 1800


@dataclass(frozen=True)
class ScanAppContext:
    staff_id: int


@dataclass(frozen=True)
class Actor:
    staff_id: int
    roles: frozenset[str]


@dataclass(frozen=True)
class ScanInput:
    request_id: UUID | str
    token: str = field(repr=False)
    meal_type_id: int
    scanner_code: str
    quantity: int = 1
    authorization_id: int | None = None
    visitor_details: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ScanResult:
    approved: bool
    code: str
    request_id: str | None
    serving_id: int | None = None
    meal_ids: tuple[int, ...] = ()
    duplicate: bool = False
