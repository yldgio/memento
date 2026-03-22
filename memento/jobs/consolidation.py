"""Consolidation job — extracts memories from ended sessions via LLM.

This is the highest-risk component: memory quality is determined here.
Implements FR-CON-01–08, SEC-01, SEC-02.

The single public entry-point is :func:`run_consolidation`, which accepts
a :class:`~memento.memory.schema.SessionLog` and routes the resulting
:class:`~memento.memory.schema.MemoryObject` instances to the correct
backend store (Mem0 for project-scoped, Graphiti for org-scoped).
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from memento.config import get_settings
from memento.memory.schema import (
    Cell,
    Lifetime,
    MemoryObject,
    Observation,
    Provenance,
    Scope,
    SessionLog,
    SessionStatus,
    TrustTier,
)
from memento.stores.base import MemoryResult, MemoryStore, SearchFilters

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_OBSERVATION_BYTES = 10_240  # 10 KB

_INJECTION_RE = re.compile(
    r"(?i)"
    r"(ignore\s+previous|system\s+prompt|disregard|"
    r"forget\s+everything|new\s+instructions|override\s+instructions)"
)

_B64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)

_DEDUP_SIMILARITY_THRESHOLD = 0.9

_EXTRACTION_PROMPT = (
    "You are a knowledge extraction engine. Given a session log "
    "from an AI coding agent,\n"
    "extract discrete, actionable learnings. For each learning:\n"
    "1. State the learning as a single clear sentence\n"
    "2. Assign a confidence score (0.0-1.0) based on evidence strength\n"
    "3. Classify scope: \"project\" (specific to this codebase) "
    "or \"org\" (applicable broadly)\n"
    "4. Assign tags: [\"pattern\", \"anti-pattern\", \"gotcha\", "
    "\"decision\", \"error\", ...]\n\n"
    "Return JSON array of candidates."
)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class ConsolidationResult:
    """Summary of a single consolidation run."""

    batch_id: str
    session_id: str
    promoted: int = 0
    unverified: int = 0
    duplicates: int = 0
    skipped_injection: int = 0
    skipped_length: int = 0
    skipped_entropy: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Injection / security heuristics  (SEC-01, SEC-02)
# ---------------------------------------------------------------------------


def _sanitize_text(text: str) -> str:
    """Normalise Unicode and strip control / zero-width characters.

    This prevents injection-pattern bypass via invisible characters
    (e.g. zero-width spaces, null bytes, combining marks).
    """
    # NFC normalisation collapses decomposed forms
    normalised = unicodedata.normalize("NFC", text)
    # Remove C0/C1 control chars, zero-width chars, and soft hyphens
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
                  r"\u200b-\u200f\u2028-\u202f"
                  r"\u2060-\u206f\ufeff\u00ad]+", "", normalised)


def _exceeds_length(content: str) -> bool:
    """Return *True* if *content* exceeds the 10 KB safety limit."""
    return len(content.encode("utf-8")) > _MAX_OBSERVATION_BYTES


def _matches_injection_pattern(content: str) -> bool:
    """Return *True* if *content* contains known prompt-injection patterns.

    Text is sanitised (Unicode normalised, control chars stripped) before
    the regex is applied so that zero-width or null-byte obfuscation
    cannot bypass detection.
    """
    clean = _sanitize_text(content)
    return _INJECTION_RE.search(clean) is not None


def _is_high_entropy(content: str, *, window: int = 100) -> bool:
    """Detect Base64/encoded payloads via character-distribution heuristic.

    Scans every window of *window* characters in *content*; if >70 %
    of the characters belong to the Base64 alphabet, the observation is
    flagged.
    """
    if len(content) < window:
        return False
    for start in range(len(content) - window + 1):
        chunk = content[start : start + window]
        b64_count = sum(1 for ch in chunk if ch in _B64_CHARS)
        if b64_count / len(chunk) > 0.70:
            return True
    return False


def _filter_observations(
    observations: list[Observation],
    result: ConsolidationResult,
) -> list[Observation]:
    """Return only observations that pass all security heuristics."""
    safe: list[Observation] = []
    for obs in observations:
        if _exceeds_length(obs.content):
            result.skipped_length += 1
            logger.warning(
                "Observation skipped (length > 10 KB) in session %s",
                result.session_id,
            )
            continue
        if _matches_injection_pattern(obs.content):
            result.skipped_injection += 1
            logger.warning(
                "Observation skipped (injection pattern) in session %s",
                result.session_id,
            )
            continue
        if _is_high_entropy(obs.content):
            result.skipped_entropy += 1
            logger.warning(
                "Observation skipped (high entropy) in session %s",
                result.session_id,
            )
            continue
        safe.append(obs)
    return safe


def _check_session_metadata(session: SessionLog) -> str | None:
    """Check session metadata fields for injection patterns.

    Returns a reason string if any field is suspicious, or *None*
    if all fields are safe.
    """
    for field_name, value in [
        ("task_description", session.task_description),
        ("project_id", session.project_id),
        ("agent_id", session.agent_id),
    ]:
        if _exceeds_length(value):
            return f"{field_name} exceeds length limit"
        if _matches_injection_pattern(value):
            return f"{field_name} contains injection pattern"
    return None


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------


def _build_user_message(session: SessionLog) -> str:
    """Serialise the session log into a textual prompt for the LLM."""
    parts = [
        f"Project: {session.project_id}",
        f"Agent: {session.agent_id}",
        f"Task: {session.task_description}",
        "",
        "Observations:",
    ]
    for obs in session.observations:
        tag_str = ", ".join(obs.tags) if obs.tags else ""
        parts.append(
            f"- [{obs.timestamp.isoformat()}] {obs.content}"
            + (f" (tags: {tag_str})" if tag_str else "")
        )
    return "\n".join(parts)


@dataclass
class _LLMCandidate:
    """Raw candidate extracted by the LLM (before validation)."""

    content: str
    confidence: float
    scope: str  # "project" | "org"
    tags: list[str]


async def _call_llm(
    session: SessionLog,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> list[_LLMCandidate]:
    """Call the OpenAI-compatible chat completions endpoint.

    Returns parsed candidates or an empty list on failure.
    """
    settings = get_settings()
    base_url = settings.llm_base_url.rstrip("/")
    url = f"{base_url}/v1/chat/completions"

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": _EXTRACTION_PROMPT},
            {"role": "user", "content": _build_user_message(session)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
    }

    client = http_client or httpx.AsyncClient(timeout=60.0)
    try:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("LLM call failed: %s", exc)
        return []
    finally:
        if http_client is None:
            await client.aclose()

    return _parse_llm_response(body)


def _parse_llm_response(body: dict[str, Any]) -> list[_LLMCandidate]:
    """Extract candidates from the OpenAI-compatible response JSON."""
    try:
        text = body["choices"][0]["message"]["content"]
        raw = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.error("Failed to parse LLM response: %s", exc)
        return []

    # Accept both {"candidates": [...]} and bare [...]
    items: list[dict[str, Any]]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        # Try common wrapper keys
        for key in ("candidates", "learnings", "results", "items"):
            if key in raw and isinstance(raw[key], list):
                items = raw[key]
                break
        else:
            items = []
    else:
        return []

    candidates: list[_LLMCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or item.get("learning") or ""
        if not content:
            continue
        # Safely parse confidence — reject None, NaN, non-numeric
        raw_conf = item.get("confidence")
        try:
            confidence = float(raw_conf if raw_conf is not None else 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        scope_raw = str(item.get("scope", "project")).lower()
        scope = scope_raw if scope_raw in ("project", "org") else "project"
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t) for t in tags]
        candidates.append(
            _LLMCandidate(
                content=content,
                confidence=confidence,
                scope=scope,
                tags=tags,
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


async def _is_duplicate(
    content: str,
    store: MemoryStore,
) -> MemoryObject | None:
    """Check if *content* is semantically duplicate in *store*.

    Returns the existing :class:`MemoryObject` if similarity ≥ 0.9,
    or ``None`` if no duplicate was found.
    """
    results: list[MemoryResult] = await store.search(
        content,
        SearchFilters(limit=1),
    )
    if results and results[0].score >= _DEDUP_SIMILARITY_THRESHOLD:
        return results[0].memory
    return None


async def _handle_duplicate(
    existing: MemoryObject,
    store: MemoryStore,
) -> None:
    """Increment ``session_count`` on an existing memory (read-modify-write)."""
    updated = existing.model_copy(
        update={"session_count": existing.session_count + 1},
    )
    await store.add(updated)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _scope_to_enum(scope_str: str) -> Scope:
    return Scope.ORG if scope_str == "org" else Scope.PROJECT


def _derive_cell(scope: Scope) -> Cell:
    """Map scope → default cell for Phase 0 (persistent memories)."""
    return Cell.C6 if scope is Scope.ORG else Cell.C5


def _build_memory(
    candidate: _LLMCandidate,
    *,
    session: SessionLog,
    batch_id: str,
    trust_tier: TrustTier,
    model_name: str,
) -> MemoryObject:
    """Construct a fully-provenanced :class:`MemoryObject`."""
    scope = _scope_to_enum(candidate.scope)
    return MemoryObject(
        id=str(uuid.uuid4()),
        content=candidate.content,
        scope=scope,
        lifetime=Lifetime.PERSISTENT,
        cell=_derive_cell(scope),
        confidence=candidate.confidence,
        trust_tier=trust_tier,
        provenance=Provenance(
            source_session_id=session.session_id,
            source_agent_id=session.agent_id,
            consolidation_batch_id=batch_id,
            consolidation_model=model_name,
            created_by="consolidation-job",
        ),
        tags=candidate.tags,
        project_id=session.project_id,
    )


def _select_store(
    scope: Scope,
    *,
    mem0_store: MemoryStore,
    graphiti_store: MemoryStore,
) -> MemoryStore:
    """Route to the correct backend based on scope."""
    return mem0_store if scope is Scope.PROJECT else graphiti_store


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

# Re-export SessionStore type for callers that don't want to import
# from the stores package.
from memento.stores.session_store import SessionStore  # noqa: E402


async def run_consolidation(
    session_log: SessionLog,
    mem0_store: MemoryStore,
    graphiti_store: MemoryStore,
    session_store: SessionStore,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ConsolidationResult:
    """Run the consolidation pipeline on a single session.

    Parameters
    ----------
    session_log:
        The ended session to consolidate.
    mem0_store:
        Backend for project-scoped memories.
    graphiti_store:
        Backend for org-scoped memories.
    session_store:
        SQLite session store (used to mark the session CONSOLIDATED).
    http_client:
        Optional pre-configured ``httpx.AsyncClient``; useful for
        testing and connection pooling.

    Returns
    -------
    ConsolidationResult
        Summary of what was extracted, promoted, deduplicated, and
        filtered.
    """
    settings = get_settings()
    batch_id = str(uuid.uuid4())
    result = ConsolidationResult(
        batch_id=batch_id,
        session_id=session_log.session_id,
    )

    # --- Idempotency check (Phase 0: status-based) ---
    if session_log.status == SessionStatus.CONSOLIDATED:
        logger.info(
            "Session %s already consolidated; skipping.",
            session_log.session_id,
        )
        return result

    # --- Check session metadata for injection ---
    metadata_issue = _check_session_metadata(session_log)
    if metadata_issue is not None:
        logger.warning(
            "Session %s metadata failed security check: %s",
            session_log.session_id,
            metadata_issue,
        )
        result.skipped_injection += 1
        await session_store.mark_consolidated(session_log.session_id)
        return result

    # --- Filter unsafe observations ---
    safe_observations = _filter_observations(
        session_log.observations, result
    )
    if not safe_observations:
        logger.info(
            "No safe observations in session %s; marking consolidated.",
            session_log.session_id,
        )
        await session_store.mark_consolidated(session_log.session_id)
        return result

    # Build a filtered copy for LLM input
    filtered_session = session_log.model_copy(
        update={"observations": safe_observations},
    )

    # --- LLM extraction ---
    candidates = await _call_llm(
        filtered_session, http_client=http_client
    )
    if not candidates:
        logger.warning(
            "LLM returned no candidates for session %s",
            session_log.session_id,
        )
        await session_store.mark_consolidated(session_log.session_id)
        return result

    # --- Process each candidate ---
    for candidate in candidates:
        scope = _scope_to_enum(candidate.scope)
        target_store = _select_store(
            scope,
            mem0_store=mem0_store,
            graphiti_store=graphiti_store,
        )

        # Determine trust tier
        if candidate.confidence < settings.confidence_threshold:
            trust_tier = TrustTier.UNVERIFIED
        else:
            trust_tier = TrustTier.REVIEWED

        # Deduplication check
        existing = await _is_duplicate(candidate.content, target_store)
        if existing is not None:
            result.duplicates += 1
            await _handle_duplicate(existing, target_store)
            logger.debug(
                "Duplicate detected for candidate in session %s",
                session_log.session_id,
            )
            continue

        # Build and persist
        memory = _build_memory(
            candidate,
            session=session_log,
            batch_id=batch_id,
            trust_tier=trust_tier,
            model_name=settings.llm_model,
        )
        await target_store.add(memory)

        if trust_tier == TrustTier.REVIEWED:
            result.promoted += 1
        else:
            result.unverified += 1

    # --- Mark session consolidated ---
    await session_store.mark_consolidated(session_log.session_id)

    logger.info(
        "Consolidation complete for session %s: "
        "promoted=%d, unverified=%d, duplicates=%d, "
        "skipped(injection=%d, length=%d, entropy=%d)",
        session_log.session_id,
        result.promoted,
        result.unverified,
        result.duplicates,
        result.skipped_injection,
        result.skipped_length,
        result.skipped_entropy,
    )
    return result
