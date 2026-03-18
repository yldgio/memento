"""Unit tests for memento.memory.schema module."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memento.memory.schema import (
    AntiPattern,
    AppliesTo,
    CausedBy,
    Cell,
    Incident,
    Learning,
    Lifetime,
    MemoryObject,
    Observation,
    Policy,
    PromotionDecision,
    Provenance,
    Scope,
    SessionLog,
    SessionStatus,
    Supersedes,
    TrustTier,
    edge_type_map,
)

# =============================================================================
# Enum tests
# =============================================================================


class TestTrustTierOrdering:
    """TrustTier enum must support ordinal comparisons."""

    def test_unverified_less_than_reviewed(self) -> None:
        assert TrustTier.UNVERIFIED < TrustTier.REVIEWED

    def test_reviewed_less_than_curated(self) -> None:
        assert TrustTier.REVIEWED < TrustTier.CURATED

    def test_unverified_less_than_curated(self) -> None:
        assert TrustTier.UNVERIFIED < TrustTier.CURATED

    def test_curated_greater_than_unverified(self) -> None:
        assert TrustTier.CURATED > TrustTier.UNVERIFIED

    def test_reviewed_greater_than_unverified(self) -> None:
        assert TrustTier.REVIEWED > TrustTier.UNVERIFIED

    def test_equality(self) -> None:
        assert TrustTier.REVIEWED == TrustTier.REVIEWED

    def test_ordering_chain(self) -> None:
        """Full ordering chain should hold."""
        assert TrustTier.UNVERIFIED < TrustTier.REVIEWED < TrustTier.CURATED


class TestScopeEnum:
    """Scope enum values."""

    def test_values(self) -> None:
        assert Scope.PROJECT == "PROJECT"
        assert Scope.ORG == "ORG"


class TestLifetimeEnum:
    """Lifetime enum values."""

    def test_values(self) -> None:
        assert Lifetime.TEMPORAL == "TEMPORAL"
        assert Lifetime.PERSISTENT == "PERSISTENT"


class TestCellEnum:
    """Cell enum values."""

    def test_values(self) -> None:
        assert Cell.C2 == "C2"
        assert Cell.C3 == "C3"
        assert Cell.C5 == "C5"
        assert Cell.C6 == "C6"

    def test_all_cells_present(self) -> None:
        assert len(Cell) == 4


class TestSessionStatusEnum:
    """SessionStatus enum values."""

    def test_all_statuses(self) -> None:
        assert SessionStatus.ACTIVE == "ACTIVE"
        assert SessionStatus.ENDED == "ENDED"
        assert SessionStatus.TIMED_OUT == "TIMED_OUT"
        assert SessionStatus.CONSOLIDATED == "CONSOLIDATED"

    def test_count(self) -> None:
        assert len(SessionStatus) == 4


# =============================================================================
# MemoryObject tests
# =============================================================================


def _make_provenance(**overrides: object) -> Provenance:
    """Helper to create a valid Provenance instance."""
    defaults: dict = {
        "source_session_id": "sess-001",
        "source_agent_id": "agent-001",
        "consolidation_batch_id": "batch-001",
        "consolidation_model": "gpt-4o",
        "promotion_decisions": [],
        "created_by": "consolidation-job",
    }
    defaults.update(overrides)
    return Provenance(**defaults)


def _make_memory_object(**overrides: object) -> MemoryObject:
    """Helper to create a valid MemoryObject instance."""
    now = datetime.now(UTC)
    defaults: dict = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "content": "Always run migrations before deploying",
        "scope": Scope.PROJECT,
        "lifetime": Lifetime.PERSISTENT,
        "cell": Cell.C5,
        "confidence": 0.85,
        "trust_tier": TrustTier.REVIEWED,
        "provenance": _make_provenance(),
        "tags": ["deployment", "database"],
        "project_id": "proj-001",
        "created_at": now,
        "valid_from": now,
        "valid_to": None,
        "superseded_by": None,
        "session_count": 3,
    }
    defaults.update(overrides)
    return MemoryObject(**defaults)


class TestMemoryObjectValid:
    """Valid MemoryObject construction tests."""

    def test_basic_construction(self) -> None:
        mem = _make_memory_object()
        assert mem.id == "550e8400-e29b-41d4-a716-446655440000"
        assert mem.content == "Always run migrations before deploying"
        assert mem.scope == Scope.PROJECT
        assert mem.confidence == 0.85
        assert mem.trust_tier == TrustTier.REVIEWED

    def test_org_scope_no_project(self) -> None:
        """Org-scoped memories may have project_id=None."""
        mem = _make_memory_object(scope=Scope.ORG, project_id=None, cell=Cell.C6)
        assert mem.scope == Scope.ORG
        assert mem.project_id is None

    def test_default_tags_empty(self) -> None:
        mem = _make_memory_object(tags=[])
        assert mem.tags == []

    def test_confidence_boundaries(self) -> None:
        """Confidence at exactly 0.0 and 1.0 should be valid."""
        mem_low = _make_memory_object(confidence=0.0)
        assert mem_low.confidence == 0.0

        mem_high = _make_memory_object(confidence=1.0)
        assert mem_high.confidence == 1.0


class TestMemoryObjectInvalid:
    """Invalid MemoryObject construction should raise ValidationError."""

    def test_confidence_above_one(self) -> None:
        with pytest.raises(ValidationError):
            _make_memory_object(confidence=1.1)

    def test_confidence_below_zero(self) -> None:
        with pytest.raises(ValidationError):
            _make_memory_object(confidence=-0.1)

    def test_missing_required_field(self) -> None:
        with pytest.raises(ValidationError):
            MemoryObject(id="test")  # type: ignore[call-arg]

    def test_invalid_scope(self) -> None:
        with pytest.raises(ValidationError):
            _make_memory_object(scope="INVALID")

    def test_negative_session_count(self) -> None:
        with pytest.raises(ValidationError):
            _make_memory_object(session_count=-1)


# =============================================================================
# Provenance tests
# =============================================================================


class TestProvenance:
    """Provenance model tests."""

    def test_basic_construction(self) -> None:
        prov = _make_provenance()
        assert prov.source_session_id == "sess-001"
        assert prov.created_by == "consolidation-job"
        assert prov.promotion_decisions == []

    def test_with_promotion_decisions(self) -> None:
        now = datetime.now(UTC)
        decisions = [
            PromotionDecision(
                from_tier=TrustTier.UNVERIFIED,
                to_tier=TrustTier.REVIEWED,
                decided_by="consolidation-job",
                decided_at=now,
                reason="Confidence threshold met",
            ),
        ]
        prov = _make_provenance(promotion_decisions=decisions)
        assert len(prov.promotion_decisions) == 1
        assert prov.promotion_decisions[0].from_tier == TrustTier.UNVERIFIED
        assert prov.promotion_decisions[0].to_tier == TrustTier.REVIEWED

    def test_serialization_round_trip(self) -> None:
        """Provenance should survive JSON serialization and deserialization."""
        now = datetime.now(UTC)
        decisions = [
            PromotionDecision(
                from_tier=TrustTier.UNVERIFIED,
                to_tier=TrustTier.REVIEWED,
                decided_by="consolidation-job",
                decided_at=now,
                reason="High confidence",
            ),
            PromotionDecision(
                from_tier=TrustTier.REVIEWED,
                to_tier=TrustTier.CURATED,
                decided_by="admin",
                decided_at=now,
                reason="Manual approval",
            ),
        ]
        prov = _make_provenance(promotion_decisions=decisions)

        json_str = prov.model_dump_json()
        parsed = json.loads(json_str)
        restored = Provenance.model_validate(parsed)

        assert restored.source_session_id == prov.source_session_id
        assert len(restored.promotion_decisions) == 2
        assert restored.promotion_decisions[0].reason == "High confidence"
        assert restored.promotion_decisions[1].decided_by == "admin"


# =============================================================================
# SessionLog tests
# =============================================================================


class TestSessionLog:
    """SessionLog model tests."""

    def test_basic_construction(self) -> None:
        now = datetime.now(UTC)
        log = SessionLog(
            session_id="sess-001",
            project_id="proj-001",
            agent_id="agent-001",
            task_description="Fix authentication bug",
            started_at=now,
            status=SessionStatus.ACTIVE,
        )
        assert log.session_id == "sess-001"
        assert log.status == SessionStatus.ACTIVE
        assert log.observations == []
        assert log.ended_at is None

    def test_with_observations(self) -> None:
        now = datetime.now(UTC)
        obs = [
            Observation(
                timestamp=now,
                content="Found SQL injection in login endpoint",
                tags=["security", "sql-injection"],
                context={"file": "auth.py", "line": 42},
            ),
            Observation(
                timestamp=now,
                content="Applied parameterized query fix",
                tags=["security", "fix"],
            ),
        ]
        log = SessionLog(
            session_id="sess-002",
            project_id="proj-001",
            agent_id="agent-002",
            task_description="Security audit",
            started_at=now,
            observations=obs,
            status=SessionStatus.ENDED,
        )
        assert len(log.observations) == 2
        assert log.observations[0].context == {"file": "auth.py", "line": 42}
        assert log.observations[1].context is None

    def test_serialization_round_trip(self) -> None:
        """SessionLog should survive JSON serialization and deserialization."""
        now = datetime.now(UTC)
        log = SessionLog(
            session_id="sess-003",
            project_id="proj-002",
            agent_id="agent-003",
            task_description="Refactor database layer",
            started_at=now,
            ended_at=now,
            observations=[
                Observation(
                    timestamp=now,
                    content="Extracted repository pattern",
                    tags=["refactoring"],
                ),
            ],
            status=SessionStatus.CONSOLIDATED,
        )

        json_str = log.model_dump_json()
        parsed = json.loads(json_str)
        restored = SessionLog.model_validate(parsed)

        assert restored.session_id == "sess-003"
        assert restored.status == SessionStatus.CONSOLIDATED
        assert len(restored.observations) == 1
        assert restored.observations[0].content == "Extracted repository pattern"


# =============================================================================
# MemoryObject serialization round-trip
# =============================================================================


class TestMemoryObjectSerialization:
    """Full JSON round-trip for MemoryObject."""

    def test_round_trip(self) -> None:
        mem = _make_memory_object()
        json_str = mem.model_dump_json()
        parsed = json.loads(json_str)
        restored = MemoryObject.model_validate(parsed)

        assert restored.id == mem.id
        assert restored.content == mem.content
        assert restored.scope == mem.scope
        assert restored.lifetime == mem.lifetime
        assert restored.cell == mem.cell
        assert restored.confidence == mem.confidence
        assert restored.trust_tier == mem.trust_tier
        assert restored.tags == mem.tags
        assert restored.project_id == mem.project_id
        assert restored.session_count == mem.session_count

    def test_model_dump_dict(self) -> None:
        """model_dump() should return a dict representation."""
        mem = _make_memory_object()
        data = mem.model_dump()
        assert isinstance(data, dict)
        assert data["id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert data["scope"] == "PROJECT"


# =============================================================================
# Graphiti entity type tests
# =============================================================================


class TestIncident:
    """Incident model tests."""

    def test_basic(self) -> None:
        inc = Incident(
            severity="critical",
            status="active",
            affected_projects=["proj-001"],
        )
        assert inc.severity == "critical"
        assert inc.resolved_at is None
        assert inc.root_cause is None

    def test_resolved(self) -> None:
        now = datetime.now(UTC)
        inc = Incident(
            severity="high",
            status="resolved",
            affected_projects=["proj-001", "proj-002"],
            resolved_at=now,
            root_cause="Memory leak in cache layer",
        )
        assert inc.status == "resolved"
        assert inc.root_cause == "Memory leak in cache layer"


class TestLearning:
    """Learning model tests."""

    def test_basic(self) -> None:
        learning = Learning(
            category="best-practice",
            confidence=0.9,
            session_count=5,
            applicable_stacks=["python", "fastapi"],
        )
        assert learning.category == "best-practice"
        assert learning.session_count == 5

    def test_confidence_constraint(self) -> None:
        with pytest.raises(ValidationError):
            Learning(
                category="pattern",
                confidence=1.5,
                session_count=1,
            )

    def test_session_count_minimum(self) -> None:
        with pytest.raises(ValidationError):
            Learning(
                category="gotcha",
                confidence=0.5,
                session_count=0,
            )


class TestAntiPattern:
    """AntiPattern model tests."""

    def test_basic(self) -> None:
        ap = AntiPattern(
            pattern_description="Using SELECT * in production queries",
            why_harmful="Causes excessive memory and network usage",
            recommended_alternative="Explicitly list required columns",
            evidence_count=3,
        )
        assert ap.evidence_count == 3

    def test_evidence_count_minimum(self) -> None:
        with pytest.raises(ValidationError):
            AntiPattern(
                pattern_description="test",
                why_harmful="test",
                recommended_alternative="test",
                evidence_count=0,
            )


class TestPolicy:
    """Policy model tests."""

    def test_basic(self) -> None:
        policy = Policy(
            domain="security",
            mandatory=True,
            source="human",
            source_document="SEC-001.md",
        )
        assert policy.mandatory is True
        assert policy.source_document == "SEC-001.md"

    def test_no_source_document(self) -> None:
        policy = Policy(
            domain="architecture",
            mandatory=False,
            source="analytics-job",
        )
        assert policy.source_document is None


# =============================================================================
# Graphiti edge type tests
# =============================================================================


class TestSupersedes:
    """Supersedes edge tests."""

    def test_basic(self) -> None:
        now = datetime.now(UTC)
        edge = Supersedes(reason="Updated with new evidence", superseded_at=now)
        assert edge.reason == "Updated with new evidence"


class TestCausedBy:
    """CausedBy edge tests."""

    def test_basic(self) -> None:
        edge = CausedBy(confidence=0.95)
        assert edge.confidence == 0.95

    def test_confidence_constraint(self) -> None:
        with pytest.raises(ValidationError):
            CausedBy(confidence=1.5)


class TestAppliesTo:
    """AppliesTo edge tests."""

    def test_project_scope(self) -> None:
        edge = AppliesTo(scope="project:proj-001")
        assert edge.scope == "project:proj-001"

    def test_org_wide_scope(self) -> None:
        edge = AppliesTo(scope="org-wide")
        assert edge.scope == "org-wide"

    def test_stack_scope(self) -> None:
        edge = AppliesTo(scope="stack:python")
        assert edge.scope == "stack:python"


# =============================================================================
# Edge type map tests
# =============================================================================


class TestEdgeTypeMap:
    """Edge type map structure tests."""

    def test_all_entries_present(self) -> None:
        assert ("Incident", "AntiPattern") in edge_type_map
        assert ("Learning", "Learning") in edge_type_map
        assert ("AntiPattern", "Learning") in edge_type_map
        assert ("Policy", "Learning") in edge_type_map
        assert ("Policy", "Incident") in edge_type_map
        assert ("Learning", "Entity") in edge_type_map

    def test_entry_count(self) -> None:
        assert len(edge_type_map) == 6

    def test_caused_by_mapping(self) -> None:
        assert edge_type_map[("Incident", "AntiPattern")] == ["CausedBy"]

    def test_supersedes_mappings(self) -> None:
        assert edge_type_map[("Learning", "Learning")] == ["Supersedes"]
        assert edge_type_map[("AntiPattern", "Learning")] == ["Supersedes"]

    def test_applies_to_mappings(self) -> None:
        assert edge_type_map[("Policy", "Learning")] == ["AppliesTo"]
        assert edge_type_map[("Policy", "Incident")] == ["AppliesTo"]
        assert edge_type_map[("Learning", "Entity")] == ["AppliesTo"]
