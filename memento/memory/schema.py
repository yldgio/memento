"""Data models for the Memento memory system.

Implements the data model from TRD §4.1–§4.7, including:
- Core enums: Scope, Lifetime, Cell, TrustTier, SessionStatus
- Core models: MemoryObject, Provenance, PromotionDecision, SessionLog, Observation
- Graphiti entity types: Incident, Learning, AntiPattern, Policy
- Graphiti edge types: Supersedes, CausedBy, AppliesTo
- Edge type mapping: edge_type_map
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field

# =============================================================================
# §4.1 Enumerations
# =============================================================================


class Scope(enum.StrEnum):
    """Memory ownership scope."""

    PROJECT = "PROJECT"
    ORG = "ORG"


class Lifetime(enum.StrEnum):
    """Memory temporal lifetime."""

    TEMPORAL = "TEMPORAL"
    PERSISTENT = "PERSISTENT"


class Cell(enum.StrEnum):
    """Memory taxonomy cell (from the 3×2 grid).

    C2 = Temporal + Project-shared
    C3 = Temporal + Org-wide
    C5 = Persistent + Project-shared
    C6 = Persistent + Org-wide
    """

    C2 = "C2"
    C3 = "C3"
    C5 = "C5"
    C6 = "C6"


class TrustTier(int, enum.Enum):
    """Memory trust level with ordinal comparison.

    Ordering: UNVERIFIED < REVIEWED < CURATED
    Uses int enum so comparison operators work naturally.
    """

    UNVERIFIED = 0
    REVIEWED = 1
    CURATED = 2


class SessionStatus(enum.StrEnum):
    """Session lifecycle status."""

    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    TIMED_OUT = "TIMED_OUT"
    CONSOLIDATED = "CONSOLIDATED"


# =============================================================================
# §4.2 Provenance models
# =============================================================================


def _utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


def _require_utc(v: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware datetimes to UTC.

    Raises:
        ValueError: If *v* has no timezone information (``tzinfo is None``).
    """
    if v.tzinfo is None:
        raise ValueError(
            "Timestamp must be timezone-aware. "
            "Pass a UTC datetime, e.g. datetime.now(UTC) or "
            "datetime(..., tzinfo=UTC)."
        )
    return v.astimezone(UTC)


# Annotated datetime type that enforces UTC on every timestamp field.
# Naive datetimes raise ValidationError; tz-aware datetimes are normalised to UTC.
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class PromotionDecision(BaseModel):
    """Record of a trust-tier promotion decision."""

    from_tier: TrustTier
    to_tier: TrustTier
    decided_by: str
    decided_at: UtcDatetime
    reason: str


class Provenance(BaseModel):
    """Full origin chain for a memory object (TRD §4.2)."""

    source_session_id: str
    source_agent_id: str
    consolidation_batch_id: str
    consolidation_model: str
    promotion_decisions: list[PromotionDecision] = Field(default_factory=list)
    created_by: str  # "consolidation-job" | "analytics-job" | "admin"


# =============================================================================
# §4.1 MemoryObject
# =============================================================================


class MemoryObject(BaseModel):
    """The fundamental memory unit in Memento (TRD §4.1)."""

    id: str = Field(description="UUID v4 identifier")
    content: str = Field(description="Natural language memory content")
    scope: Scope
    lifetime: Lifetime
    cell: Cell
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0.0–1.0")
    trust_tier: TrustTier = TrustTier.UNVERIFIED
    provenance: Provenance
    tags: list[str] = Field(default_factory=list)
    project_id: str | None = None
    created_at: UtcDatetime = Field(default_factory=_utc_now)
    valid_from: UtcDatetime = Field(default_factory=_utc_now)
    valid_to: UtcDatetime | None = None
    superseded_by: str | None = None
    session_count: int = Field(default=1, ge=0)


# =============================================================================
# §4.3 SessionLog and Observation
# =============================================================================


class Observation(BaseModel):
    """A single observation within a session log."""

    timestamp: UtcDatetime
    content: str
    tags: list[str] = Field(default_factory=list)
    context: dict[str, Any] | None = None


class SessionLog(BaseModel):
    """Raw session data that feeds the consolidation pipeline (TRD §4.3)."""

    session_id: str = Field(description="UUID v4 identifier")
    project_id: str
    agent_id: str
    task_description: str
    started_at: UtcDatetime = Field(default_factory=_utc_now)
    ended_at: UtcDatetime | None = None
    observations: list[Observation] = Field(default_factory=list)
    status: SessionStatus = SessionStatus.ACTIVE


# =============================================================================
# §4.5 Graphiti Entity Types
# =============================================================================


class Incident(BaseModel):
    """Graphiti entity: a tracked incident."""

    severity: str = Field(description="critical | high | medium | low")
    status: str = Field(description="active | resolved | post-mortem-complete")
    affected_projects: list[str] = Field(default_factory=list)
    resolved_at: UtcDatetime | None = None
    root_cause: str | None = None


class Learning(BaseModel):
    """Graphiti entity: an extracted learning."""

    category: str = Field(
        description="pattern | anti-pattern | gotcha | best-practice | decision"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    session_count: int = Field(ge=1)
    applicable_stacks: list[str] = Field(default_factory=list)


class AntiPattern(BaseModel):
    """Graphiti entity: a recognized anti-pattern."""

    pattern_description: str
    why_harmful: str
    recommended_alternative: str
    evidence_count: int = Field(ge=1)


class Policy(BaseModel):
    """Graphiti entity: an organizational or project policy."""

    domain: str = Field(
        description="security | architecture | coding-standard | operations"
    )
    mandatory: bool
    source: str = Field(description="human | analytics-job | consolidation-job")
    source_document: str | None = None


# =============================================================================
# §4.6 Graphiti Edge Types
# =============================================================================


class Supersedes(BaseModel):
    """Graphiti edge: one entity replaces another."""

    reason: str
    superseded_at: UtcDatetime


class CausedBy(BaseModel):
    """Graphiti edge: causal relationship between entities."""

    confidence: float = Field(ge=0.0, le=1.0)


class AppliesTo(BaseModel):
    """Graphiti edge: scope of applicability."""

    scope: str = Field(description="project:<id> | stack:<name> | org-wide")


# =============================================================================
# §4.7 Edge Type Map
# =============================================================================

edge_type_map: dict[tuple[str, str], list[str]] = {
    ("Incident", "AntiPattern"): ["CausedBy"],
    ("Learning", "Learning"): ["Supersedes"],
    ("AntiPattern", "Learning"): ["Supersedes"],
    ("Policy", "Learning"): ["AppliesTo"],
    ("Policy", "Incident"): ["AppliesTo"],
    ("Learning", "Entity"): ["AppliesTo"],
}
