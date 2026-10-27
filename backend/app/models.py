"""Typed records for KEEP. One schema for every agent (fixes LORE's two-format problem)."""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Status(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    OVERRIDDEN = "overridden"


class SourceType(str, Enum):
    PR = "pr"
    ISSUE = "issue"
    COMMENT = "comment"
    COMMIT = "commit"


class Origin(str, Enum):
    """Where the evidence came from. Code-only finds start at MEDIUM confidence."""

    DISCUSSION = "discussion"
    CODE = "code"
    BOTH = "both"


class Source(BaseModel):
    type: SourceType
    url: HttpUrl
    ref: str = Field(description="PR/issue number or commit sha, e.g. '42' or 'a1b2c3d'")


class HistoryEntry(BaseModel):
    at: datetime
    change: str


class DecisionIn(BaseModel):
    """What a caller (n8n / Decision Extractor) sends. No ID, status or history: code sets those."""

    title: str = Field(min_length=3, max_length=120)
    decision: str = Field(min_length=3)
    rejected: str | None = None
    reason: str = Field(min_length=3)
    future_implication: str = Field(min_length=3)
    source: Source
    decided_by: list[str] = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    origin: Origin = Origin.DISCUSSION
    security_relevant: bool = False


class Decision(DecisionIn):
    """A stored decision. Fields below are owned by code, never by Claude."""

    id: str
    status: Status = Status.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    depends_on: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    needs_sync: bool = False
