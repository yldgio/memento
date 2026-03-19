# Memento — Implementation Tasks

> **Scope**: Phase 0 detailed, Phases 1–4 outline
> **Source**: [TRD](./TRD.md) §5, §6, §9, §12 · [Plan](./plan.md)
> **Notation**: `→` = depends on · `‖` = parallelizable · `🔴🟡🟢` = risk level

---

## Phase 0 — Foundation

**Goal**: Prove the core loop end-to-end — agent starts session → logs observations → consolidation extracts learnings → next session gets enriched context.

**Success criterion**: Second task on the same project demonstrably uses knowledge from the first task's session log.

**Requirements in scope**: FR-MCP-01–06, FR-CTX-01, FR-CTX-06, FR-LOG-01, FR-LOG-03–05, FR-CON-01–08, FR-API-08, SEC-01, SEC-02.

---

### Task Graph

```text
P0-T01 (project scaffold)
  ├──► P0-T02 (config module) ─────────────────────────────────────────┐
  ├──► P0-T03 (data model) ──┬──► P0-T05 (graphiti store) ──┐         │
  │                           ├──► P0-T06 (mem0 store)    ───┤         │
  │                           └──► P0-T07 (session store) ───┤         │
  └──► P0-T04 (docker compose) ‖                             │         │
                                                              ▼         ▼
                                                     P0-T08 (MCP server)
                                                              │
                                              P0-T09 (consolidation job)
                                                              │
                                              P0-T10 (scheduler)
                                                              │
                                              P0-T11 (health endpoint)
                                                              │
                                              P0-T12 (integration tests)
                                                              │
                                              P0-T13 (e2e validation)
```

---

### P0-T01 · Project Scaffold 🟢

**Status**: Complete — verified by Batch A evidence below

**Creates**: Project structure, Python packaging, dev tooling

**Depends on**: Nothing (first task)

**Files to create**:
```
memento/
├── pyproject.toml            # Python project config (hatchling or setuptools)
├── Dockerfile                # Multi-stage build: deps → app
├── .env.example              # Template for required env vars
├── .gitignore                # Python + Docker ignores
├── memento/
│   ├── __init__.py
│   ├── main.py               # FastAPI app entrypoint (REST + MCP)
│   └── scheduler.py          # Scheduler entrypoint (consolidation cron)
└── tests/
    ├── __init__.py
    ├── conftest.py            # Shared fixtures (test client, mock LLM, etc.)
    └── unit/
        └── __init__.py
```

**Acceptance criteria**:
- `pip install -e .` succeeds
- `python -m memento.main` starts without error (may exit immediately — no routes yet)
- `docker build .` produces a valid image
- `pytest` runs with zero tests collected, zero errors

**Details**:
- Python ≥ 3.11
- Dependencies: `fastapi`, `uvicorn`, `mcp[server]`, `graphiti-core`, `mem0ai`, `pydantic`, `httpx`
- Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- Entrypoint in `memento/main.py`: bare FastAPI app, no routes yet
- Dockerfile: multi-stage (builder with deps → slim runtime)

---

### P0-T02 · Configuration Module 🟢

**Status**: Complete — verified by Batch A evidence below

**Creates**: `memento/config.py`

**Depends on**: P0-T01

**Implements**: §10 Configuration (all `MEMENTO_*` env vars)

**Acceptance criteria**:
- All env vars from TRD §10 are represented as typed fields
- Missing required vars (`MEMENTO_LLM_API_KEY`) raise clear error at startup
- Defaults match TRD §10 table
- Unit tests validate defaults, required field enforcement, type coercion

**Details**:
- Use `pydantic-settings` (`BaseSettings`) for env var parsing
- Single `Settings` class, singleton via `@lru_cache`
- Fields:
  ```python
  llm_base_url: str = "https://api.openai.com/v1"
  llm_model: str = "gpt-4o"
  llm_api_key: str                    # required, no default
  falkordb_host: str = "localhost"
  falkordb_port: int = 6379
  confidence_threshold: float = 0.6
  session_timeout: int = 3600
  max_context_tokens: int = 4000
  max_memories_per_query: int = 20
  consolidation_schedule: str = "*/30 * * * *"
  analytics_schedule: str = "0 2 * * 0"
  api_port: int = 8080
  mcp_port: int = 8081
  log_level: str = "INFO"
  api_key: str | None = None          # optional REST auth
  mcp_token: str | None = None        # optional MCP auth
  org_promotion_min_sessions: int = 2
  ```
- Prefix: `MEMENTO_` (pydantic-settings `env_prefix`)

**Tests**: `tests/unit/test_config.py`
- Test defaults match TRD
- Test missing required key raises `ValidationError`
- Test env override works

---

### P0-T03 · Data Model 🟡

**Status**: Complete — verified by Batch A evidence below

**Creates**: `memento/memory/schema.py`, `memento/memory/__init__.py`

**Depends on**: P0-T01

**Implements**: TRD §4 (MemoryObject, Provenance, SessionLog, Observation, enums, Graphiti entity/edge types)

**Acceptance criteria**:
- All data classes from §4.1–§4.7 are defined as Pydantic models
- Enums: `Scope`, `Lifetime`, `Cell`, `TrustTier`, `SessionStatus`
- `SessionStatus` includes `ACTIVE | ENDED | TIMED_OUT | CONSOLIDATED`
- `TrustTier` ordering: `UNVERIFIED < REVIEWED < CURATED` (comparable)
- Graphiti entity types: `Incident`, `Learning`, `AntiPattern`, `Policy`
- Graphiti edge types: `Supersedes`, `CausedBy`, `AppliesTo`
- `edge_type_map` dict matching §4.7
- All models serializable to/from JSON
- Unit tests for enum ordering, serialization round-trip, validation constraints

**Details**:
- `MemoryObject.confidence` constrained to `[0.0, 1.0]` via `Field(ge=0, le=1)`
- `Provenance.promotion_decisions` is `list[PromotionDecision]`
- `SessionLog.observations` is `list[Observation]`
- UUID fields use `str` (not `uuid.UUID`) for JSON compatibility
- Timestamps use `datetime` with UTC enforcement

**Tests**: `tests/unit/test_schema.py`
- Enum comparisons (`TrustTier.REVIEWED > TrustTier.UNVERIFIED`)
- Valid/invalid MemoryObject construction
- Provenance serialization round-trip
- SessionLog with observations

**Parallelizable with**: P0-T02, P0-T04

---

### P0-T04 · Docker Compose Stack 🟢

**Status**: Complete — verified by Batch A evidence below

**Creates**: `docker-compose.yml`, updates `Dockerfile`

**Depends on**: P0-T01

**Implements**: TRD §9.1, §9.2

**Acceptance criteria**:
- `docker compose up` starts 3 services: `memento-api`, `falkordb`, `scheduler`
- FalkorDB is reachable on port 6379
- Memento API responds on port 8080
- Port `8081` is reserved/published for the future MCP streamable-http server delivered in `P0-T08`
- Named volumes persist `falkordb-data`, `mem0-data`, and `memento-data` (SQLite session store)
- `.env.example` covers all required vars

**Details**:
- Exact YAML from TRD §9.2 as starting point
- FalkorDB image: `falkordb/falkordb:latest`
- Both `memento-api` and `scheduler` build from same Dockerfile, different `command`
- Health check on `memento-api`: `curl -f http://localhost:8080/health` (once health endpoint exists)
- No MCP server or routes needed yet — just the infrastructure

**Parallelizable with**: P0-T02, P0-T03

---

### P0-T05 · Graphiti Store Wrapper 🟡

**Creates**: `memento/stores/graphiti_store.py`, `memento/stores/__init__.py`, `memento/stores/base.py`

**Depends on**: P0-T03 (needs MemoryObject, Provenance, Graphiti entity types)

**Implements**: TRD §6.3 `MemoryStore` protocol, Graphiti-specific implementation

**Acceptance criteria**:
- `MemoryStore` protocol defined in `base.py` matching TRD §6.3
- `GraphitiStore` implements all 5 methods: `add`, `search`, `get`, `invalidate`, `update_trust_tier`
- Custom entity types registered: `Incident`, `Learning`, `AntiPattern`, `Policy`
- Custom edge types registered: `Supersedes`, `CausedBy`, `AppliesTo`
- `edge_type_map` applied on initialization
- `add_episode` called with correct parameters (entity_types, edge_types, reference_time)
- `search_` used with `SearchFilters` mapped to Graphiti's native filters
- Integration test with real FalkorDB (via docker compose)

**Details**:
- Constructor takes `host`, `port`, initializes `Graphiti(...)` with FalkorDB config
- `async def initialize()` — calls `graphiti.build_indices_and_constraints()`
- `add()` — creates episode via `graphiti.add_episode()`, stores MemoryObject metadata
- `search()` — maps `SearchFilters` to Graphiti `search_()` with `node_labels` filter
- `invalidate()` — sets `valid_to = now()` on the entity node
- Temporal queries respect `valid_from` / `valid_to`
- Connection pooling: reuse single Graphiti instance per store

**Tests**:
- `tests/unit/test_graphiti_store.py` — mock Graphiti client, verify correct calls
- `tests/integration/test_graphiti_store.py` — real FalkorDB (requires docker)

**Parallelizable with**: P0-T06

---

### P0-T06 · Mem0 Store Wrapper 🟡

**Creates**: `memento/stores/mem0_store.py`

**Depends on**: P0-T03 (needs MemoryObject, SearchFilters)

**Implements**: TRD §6.3 `MemoryStore` protocol, Mem0-specific implementation

**Acceptance criteria**:
- `Mem0Store` implements all 5 methods of `MemoryStore` protocol
- Namespace isolation: each project uses `user_id="project:{project_id}"`
- `add()` stores MemoryObject with metadata (provenance, trust_tier, confidence)
- `search()` uses `memory.search(query, user_id=..., limit=...)` with semantic ranking
- `get()` retrieves by ID
- `invalidate()` deletes from Mem0 (Mem0 has no temporal model — deletion is invalidation)
- `update_trust_tier()` updates metadata on the stored memory

**Details**:
- Constructor takes `Settings`, initializes `Memory.from_config(config)` with LLM provider settings
- Config maps `MEMENTO_LLM_*` to Mem0's expected config format:
  ```python
  config = {
      "llm": {
          "provider": "openai",  # or detect from base_url
          "config": {"model": settings.llm_model, "api_key": settings.llm_api_key}
      }
  }
  ```
- Metadata stored alongside memory: `confidence`, `trust_tier`, `provenance_session_id`, `tags`
- `search()` filters by `trust_tier_min` post-retrieval (Mem0 doesn't natively filter by custom metadata)

**Tests**:
- `tests/unit/test_mem0_store.py` — mock Mem0 client, verify namespace isolation
- `tests/integration/test_mem0_store.py` — real Mem0 (embedded, no external service needed)

**Parallelizable with**: P0-T05

---

### P0-T07 · Session Store 🟡

**Creates**: `memento/stores/session_store.py`

**Depends on**: P0-T03 (needs SessionLog, Observation, SessionStatus enums)

**Implements**: FR-LOG-01, FR-LOG-03, FR-LOG-04, FR-LOG-05

**Acceptance criteria**:
- `SessionStore` manages `SessionLog` objects in SQLite (shared persistent storage — required because the scheduler runs as a separate process and must discover `ENDED` sessions)
- `create_session()` → returns `SessionLog` with status `ACTIVE`; `agent_id`, `project_id`, and `task_description` are set at creation and immutable thereafter (FR-LOG-04)
- `append_observation()` → appends `Observation` to session, validates session is `ACTIVE`
- `end_session()` → sets status to `ENDED`, records `ended_at`
- `get_session()` → retrieves by ID
- `list_sessions()` → supports filtering by `project_id`, `status`
- Sessions auto-expire: a background coroutine checks active sessions against `MEMENTO_SESSION_TIMEOUT` and sets status to `TIMED_OUT`
- Append-only constraint: observations cannot be modified or deleted while session is `ACTIVE`
- Metadata immutability: attempts to change `agent_id`, `project_id`, or `task_description` after creation are rejected

**Details**:
- SQLite database at `MEMENTO_DATA_DIR/sessions.db` (volume-mounted in Docker Compose)
- Tables: `sessions` (metadata + status) and `observations` (append-only, foreign key to sessions)
- Thread-safe via SQLite WAL mode + `aiosqlite` for async access
- Timeout worker: `asyncio.create_task` at startup, checks every 60s
- The scheduler process connects to the same SQLite file to discover `ENDED` sessions

**Tests**: `tests/unit/test_session_store.py`
- Create + append + end lifecycle
- Reject append on ended session
- Timeout expiry
- Metadata immutability: reject mutation of `agent_id`, `project_id`, `task_description` after creation
- Concurrent access: two async tasks can read/write without corruption

**Parallelizable with**: P0-T05, P0-T06

---

### P0-T08 · MCP Server 🟡

**Creates**: `memento/mcp/server.py`, `memento/mcp/__init__.py`

**Depends on**: P0-T02 (config), P0-T05 (graphiti store), P0-T06 (mem0 store), P0-T07 (session store)

**Implements**: FR-MCP-01–04, FR-CTX-01, FR-CTX-06

**Acceptance criteria**:
- MCP server starts on both `stdio` and `streamable-http` transports
- 3 tools registered: `memento_context_assemble`, `memento_session_log`, `memento_session_end` (note: `memento_query` is P1 per TRD §12)
- `list_tools` returns all 3 tools with input schemas auto-generated from type hints
- `memento_context_assemble`: queries Mem0 + Graphiti, returns `ContextResponse` with source attribution, creates session
- `memento_session_log`: validates session is active, appends observation, requires `agent_id`
- `memento_session_end`: closes session, enqueues consolidation, returns session summary (observation count, timestamps, status transition, consolidation enqueue ID) per FR-MCP-04
- Error handling: invalid session_id → MCP error

**Details**:
- Use `mcp.server.fastmcp.FastMCP` with `@mcp.tool()` decorators
- Tool signatures exactly match TRD §6.1
- Context assembly logic (Phase 0 simplified):
  1. Query Mem0 for project memories (top-K by semantic relevance)
  2. Query Graphiti for org-wide learnings (valid_to is None or > now)
  3. Filter by `trust_tier >= REVIEWED`
  4. Combine, deduplicate, return structured `ContextResponse`
- Server lifecycle: starts with the FastAPI app (separate port 8081)
- Connection to stores via dependency injection (constructor receives store instances)

**Tests**:
- `tests/unit/test_mcp_server.py` — mock stores, test each tool's logic
- `tests/integration/test_mcp_server.py` — real MCP client connecting via stdio

---

### P0-T09 · Consolidation Job 🔴

**Creates**: `memento/jobs/consolidation.py`, `memento/jobs/__init__.py`

**Depends on**: P0-T02 (config), P0-T03 (data model), P0-T05 (graphiti), P0-T06 (mem0), P0-T07 (session store)

**Implements**: FR-CON-01–08, SEC-01, SEC-02

**This is the highest-risk component** — it's where memory quality is determined.

**Acceptance criteria**:
- Given a `SessionLog`, extracts discrete `MemoryObject` candidates via LLM
- Each candidate has `confidence` score assigned by LLM
- Candidates with `confidence < MEMENTO_CONFIDENCE_THRESHOLD` are stored as `UNVERIFIED` with `rejected_at` and reason
- Candidates passing threshold are promoted to `REVIEWED`
- Full `Provenance` attached to every output (session_id, agent_id, batch_id, model)
- Routing: project-specific → Mem0, cross-cutting → Graphiti (LLM decides scope)
- Deduplication: semantic similarity check against existing memories; if match, increment `session_count`
- Idempotent: re-running on same session produces no duplicates (check `consolidation_batch_id`)
- Heuristic injection checks (Phase 0 mitigation for FR-CON-08):
  - Reject observations > 10KB (anomalous length)
  - Flag observations with known injection patterns (regex: "ignore previous", "system prompt", etc.)
  - Flag observations with high entropy (Base64/encoded payloads)
- Session status set to `CONSOLIDATED` after successful run

**Details**:
- LLM prompt design (critical — this determines memory quality):
  ```
  You are a knowledge extraction engine. Given a session log from an AI coding agent,
  extract discrete, actionable learnings. For each learning:
  1. State the learning as a single clear sentence
  2. Assign a confidence score (0.0-1.0) based on evidence strength
  3. Classify scope: "project" (specific to this codebase) or "org" (applicable broadly)
  4. Assign tags: ["pattern", "anti-pattern", "gotcha", "decision", "error", ...]
  
  Return JSON array of candidates.
  ```
- LLM call via `httpx` to `MEMENTO_LLM_BASE_URL` (OpenAI-compatible chat completions)
- No vendor SDK — raw HTTP to `/v1/chat/completions`
- Deduplication: embed candidate text, cosine similarity against existing Mem0/Graphiti entries, threshold 0.9 = duplicate
- Batch ID: UUID generated per consolidation run

**Tests**:
- `tests/unit/test_consolidation.py`:
  - Mock LLM returns known candidates → verify routing to correct store
  - Below-threshold candidate → stored as UNVERIFIED
  - Duplicate detection (mock store returns existing similar memory)
  - Injection heuristic catches known patterns
  - Idempotency: second run with same batch_id produces no new entries
- `tests/integration/test_consolidation.py`:
  - Real LLM call with test session log → verify extracted memories are reasonable

---

### P0-T10 · Scheduler 🟢

**Creates**: `memento/scheduler.py` (enhance from P0-T01 stub)

**Depends on**: P0-T09 (consolidation job)

**Implements**: FR-CON-10

**Acceptance criteria**:
- Scheduler runs as separate process (`python -m memento.scheduler`)
- Executes consolidation job on cron schedule (`MEMENTO_CONSOLIDATION_SCHEDULE`)
- Connects to same SQLite session store to find sessions with status `ENDED` (not yet consolidated)
- Runs consolidation for each
- Logs each run (session_id, duration, memories extracted/promoted)

**Details**:
- Use `APScheduler` or simple `asyncio` loop with cron parsing
- Keep it simple for Phase 0 — `while True: sleep(interval); find_ended_sessions(); consolidate_each()`
- Shares same SQLite database and store initialization as `main.py` (via shared volume)
- No analytics job in Phase 0 (that's Phase 2)

**Tests**: `tests/unit/test_scheduler.py`
- Mock clock + mock consolidation → verify sessions are picked up and processed

---

### P0-T11 · Health Endpoint 🟢

**Creates**: Health check in `memento/main.py`

**Depends on**: P0-T02 (config), P0-T05 (graphiti), P0-T06 (mem0)

**Implements**: FR-API-08

**Acceptance criteria**:
- `GET /health` returns 200 with JSON: `{ "status": "ok", "components": { "graphiti": "ok|error", "mem0": "ok|error", "llm": "ok|error" } }`
- Each component is health-checked (Graphiti: ping FalkorDB, Mem0: check initialized, LLM: verify base_url reachable)
- If any component is unhealthy, top-level status is `"degraded"` (not 500 — the service still works partially)

**Details**:
- FastAPI route in `main.py`
- Timeout each health check at 5s
- Used by Docker Compose `healthcheck` directive

**Tests**: `tests/unit/test_health.py` — mock component states, verify response format

**Parallelizable with**: P0-T08, P0-T09, P0-T10

---

### P0-T12 · Integration Test Suite 🟡

**Creates**: `tests/integration/` test files

**Depends on**: P0-T08 (MCP server), P0-T09 (consolidation), P0-T11 (health)

**Implements**: TRD §11.2

**Acceptance criteria**:
- Test: MCP client connects → discovers tools → each tool responds correctly
- Test: Session lifecycle: create via `context_assemble` → log observations → end session → session status is ENDED → session summary returned
- Test: Consolidation pipeline (Mem0 path): ended session with project-specific observations → consolidation → memories appear in Mem0
- Test: Consolidation pipeline (Graphiti path): ended session with cross-cutting observations → consolidation → memories appear in Graphiti with correct attribution/tier
- Test: Context assembly retrieves both Mem0 project memories and Graphiti org-wide learnings with source attribution
- Test: Health endpoint returns component status
- All tests run against Docker Compose stack (FalkorDB real, LLM mocked)

**Details**:
- Fixture: start Docker Compose stack (or use existing), wait for health
- LLM mock: intercept `MEMENTO_LLM_BASE_URL` with `httpx` mock or local test server
- Test isolation: unique project IDs per test to avoid cross-contamination
- CI-friendly: `docker compose up -d && pytest tests/integration/ && docker compose down`

---

### P0-T13 · End-to-End Validation 🟡

**Creates**: `tests/e2e/test_core_loop.py`

**Depends on**: P0-T12 (integration tests passing)

**Implements**: Phase 0 acceptance criterion

**Acceptance criteria**:
The complete sequence works:
1. Agent (MCP client) calls `memento_context_assemble(project="test-project", task="implement auth")` → gets empty context (no prior memories)
2. Agent logs observations: `memento_session_log(session_id, "JWT tokens need 15-min expiry for this API", agent_id="test-agent")`
3. Agent ends session: `memento_session_end(session_id)`
4. Consolidation job runs (triggered or scheduled)
5. Agent starts new session: `memento_context_assemble(project="test-project", task="fix auth bug")` → **context includes the JWT expiry learning from step 2**
6. Verify: the returned context contains a memory with content referencing "JWT" and "15-min expiry"

**Details**:
- This is the proof that the core loop works
- Runs against full Docker Compose stack with real FalkorDB
- LLM can be real (slow, costs money) or mocked (fast, deterministic) — support both via env flag
- This test is the gate for Phase 0 completion

---

## Phase 1 — Core Memory Platform (outline)

**Goal**: Full 6-cell taxonomy, all MCP tools, REST API, trust tiers, temporal validity.

| Task | Description | Risk | Key FRs |
|------|-------------|------|---------|
| P1-T01 | C2 temporal state store (internal key-value with TTL) | 🟡 | C2 cell definition |
| P1-T02 | `memento_state_set` / `memento_state_get` MCP tools for C2 | 🟡 | Access control matrix |
| P1-T03 | Trust tier enforcement in context assembly | 🟡 | FR-CTX-04, SEC-02 |
| P1-T04 | Temporal validity filtering (`valid_from` / `valid_to`) | 🟡 | FR-CTX-05 |
| P1-T05 | Full provenance tracking with PromotionDecision audit trail | 🟡 | SEC-01, §4.2 |
| P1-T06 | REST Management API — memory CRUD + promote/demote/rollback | 🟡 | FR-API-01–04, FR-API-10–12 |
| P1-T07 | REST Management API — session listing + detail | 🟢 | FR-API-05–06 |
| P1-T08 | REST Management API — consolidation trigger | 🟡 | FR-API-07 |
| P1-T09 | REST API authentication (bearer token) | 🔴 | SEC-09 |
| P1-T10 | MCP authentication (bearer token on streamable-http) | 🔴 | SEC-12 |
| P1-T11 | `AGENTS.md` policy loader for context assembly | 🟡 | FR-CTX-03 |
| P1-T12 | Configurable context budget (max tokens / max memories) | 🟡 | FR-CTX-07 |
| P1-T13 | Structured context in observations | 🟢 | FR-LOG-02 |
| P1-T14 | Two-phase promotion (distinct agent_id requirement) | 🔴 | FR-CON-09, SEC-03 |
| P1-T15 | Deduplication refinement (session_count increment) | 🟡 | FR-CON-03 |
| P1-T16 | Consolidation v2: Graphiti routing for cross-cutting learnings | 🟡 | FR-CON-04 |
| P1-T17 | Persistent session store (replace in-memory with SQLite/PostgreSQL) | 🟡 | NFR-REL-01 |
| P1-T18 | Integration + E2E tests for Phase 1 | 🟡 | §11.2 |

---

## Phase 2 — Cross-Project Intelligence (outline)

**Goal**: Multi-project namespaces, analytics job, PR generation.

| Task | Description | Risk |
|------|-------------|------|
| P2-T01 | Multi-project namespace management in Mem0 + Graphiti | 🟡 |
| P2-T02 | Analytics Job: read org graph, find recurring patterns | 🟡 |
| P2-T03 | Analytics output: structured synthesis with confidence | 🟡 |
| P2-T04 | Bootstrap workflow: new project → query org graph → enriched AGENTS.md | 🟡 |
| P2-T05 | PR generation: Analytics Job opens PRs via GitHub API | 🔴 |
| P2-T06 | Flywheel: merged PR findings feed back to org graph | 🟡 |
| P2-T07 | E2E test: cross-project pattern detection | 🟡 |

---

## Phase 3 — Security & The Memento Problem (outline)

**Goal**: Harden against memory poisoning attacks.

| Task | Description | Risk |
|------|-------------|------|
| P3-T01 | Full LLM-based adversarial review in consolidation | 🔴 |
| P3-T02 | Content contradiction detection | 🔴 |
| P3-T03 | Blast radius validation test suite | 🟡 |
| P3-T04 | Project-scoped RBAC (SEC-13) | 🔴 |
| P3-T05 | Secret stripping in consolidation (SEC-11) | 🔴 |
| P3-T06 | Security test suite (SEC-T01 through SEC-T05) | 🔴 |

---

## Phase 4 — Observability & Evolution (outline)

**Goal**: Production readiness, cost management, operational tooling.

| Task | Description | Risk |
|------|-------------|------|
| P4-T01 | Prometheus metrics endpoint (`/metrics`) | 🟢 |
| P4-T02 | LLM token usage tracking per call | 🟡 |
| P4-T03 | Memory health dashboard (age, confidence, tier distribution) | 🟡 |
| P4-T04 | Knowledge decay: confidence reduction for unconfirmed memories | 🟡 |
| P4-T05 | Budget alerting (NFR-COST-03) | 🟢 |
| P4-T06 | Kubernetes Helm chart | 🟡 |
| P4-T07 | Structured JSON logging for all components | 🟢 |
| P4-T08 | Graceful degradation (LLM/Graphiti unavailable) | 🟡 |

---

## Execution Order (Phase 0)

Tasks that can be parallelized are grouped in lanes.

```
Lane A (infra):     P0-T01 ──► P0-T04 ──────────────────────────────────────┐
Lane B (config):    P0-T01 ──► P0-T02 ──────────────────────────────────────┤
Lane C (model):     P0-T01 ──► P0-T03 ──┬──► P0-T05 (graphiti) ────────────┤
                                         ├──► P0-T06 (mem0)     ────────────┤
                                         └──► P0-T07 (session)  ────────────┤
                                                                             ▼
Sequential:                                                          P0-T08 (MCP)
                                                                         │
                                                                     P0-T09 (consolidation)
                                                                         │
                                                                 P0-T10 ‖ P0-T11
                                                                         │
                                                                     P0-T12 (integration)
                                                                         │
                                                                     P0-T13 (e2e)
```

**Estimated parallelism**: After P0-T01, the next 6 tasks (T02–T07) can run in 3 parallel lanes. From T08 onward, tasks are sequential because each builds on the previous.

---

### Batch A Verification

**Status**: Complete

**Evidence**:
- `python -m pytest` → 79 passed
- `python -m ruff check memento tests` → passed
- `docker build .` → passed
- `docker compose config` → resolved `memento-api`, `falkordb`, `scheduler`, and named volumes
- `python -m memento.scheduler` → started and exited cleanly
- `tests/unit/test_schema.py` now verifies UTC enforcement for timestamp fields required by `P0-T03`

**Interpretation note**:
- `P0-T04` now explicitly records the infrastructure-only MCP expectation so Batch A verification stays aligned with the later `P0-T08` MCP implementation task.
