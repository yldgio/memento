# Project Memento — High-Level Implementation Plan

## What Is This Product?

**Memento is a containerized, LLM-agnostic, runtime-agnostic memory platform for AI coding agents.**

It is a **service**, not a library. Teams deploy it alongside their existing tooling. Any agent runtime (OpenCode, Copilot CLI, custom agents, etc.) connects to it via standard protocols. It manages memory accumulation, retrieval, and consolidation on behalf of all agents in the organization.

**Concretely, Memento is:**
- A **set of Docker containers** (deployable locally via Docker Compose, or to any cloud via Kubernetes)
- Exposing an **MCP (Model Context Protocol) server** as the primary agent integration surface — the open standard that all major agent runtimes speak natively
- With a **REST management API** for human-facing operations (review, audit, dashboard, PR generation)
- Running **scheduled background jobs** (Consolidation Job, Analytics Job) as containers in the same stack
- Backed by **Graphiti** (temporal knowledge graph, FalkorDB/Neo4j) and **Mem0** (semantic episodic memory)
- LLM calls go through a **configurable provider** (OpenAI-compatible API — supports OpenAI, Anthropic, Ollama, Azure OpenAI, any OpenAI-compatible endpoint)

**What an agent does with Memento:**
1. At session start: calls `memento://context/assemble?project=X&task=<description>` via MCP → receives pre-built context blob
2. During session: writes observations to `memento://session/log` via MCP
3. At session end (or on PR merge): triggers `memento://consolidation/run` (or the scheduler picks it up)

**What Memento does autonomously:**
- Runs the Consolidation Job: reads session logs → extracts learnings → routes to project memory (Mem0) or org graph (Graphiti)
- Runs the Analytics Job: reads across org graph → produces cross-project pattern reports → generates PRs to update AGENTS.md

**What humans control:**
- Reviewing and merging AGENTS.md update PRs (git-native, always a human in the loop for policy changes)
- Promoting memories to Curated tier (memory elevation requires human approval)
- Configuring LLM provider, thresholds, and access controls

---

## Problem Statement

Build AMP (Agent Memory Platform): an organizational memory system that lets AI coding agents accumulate, share, and retrieve knowledge across projects, sessions, and teams — without noise accumulation, memory poisoning, or context overwhelm.

The core tension: **contextual relevance vs. knowledge sharing**. Some knowledge is project-local; some is org-wide. Some expires; some persists forever. The architecture must handle all six combinations cleanly.

---

## Evidence Review: What the Research Confirms

### ✅ Validated Assumptions

| Assumption in IDEA.md | Evidence |
|---|---|
| Graphiti is the right tool for temporal org-wide memory | Confirmed: bi-temporal model (valid_at + created_at), real-time incremental updates, hybrid search (semantic + BM25 + graph traversal), custom Pydantic schemas. Neo4j or FalkorDB backend. |
| Zep/Graphiti outperforms Mem0 on temporal reasoning | Confirmed: LongMemEval with GPT-4o — Zep 63.8–71.2% vs. Mem0 ~49%. Mem0 has no native temporal validity windows. |
| Mem0 is reasonable for persistent project-scoped memory | Confirmed: 26% accuracy improvement over baseline, 91% latency reduction vs. full-context. Supports namespacing by project/agent/user. |
| Letta (MemGPT) enables stateful orchestration | Confirmed: tiered memory (core, recall, archival), self-updating memory blocks, multi-agent memory sharing. |
| Orchestrator-worker pattern is the right shape | Confirmed by Anthropic engineering blog: hybrid orchestrator (team memory) + specialist (task detail) pattern. ~90% improvement on complex tasks. 4–15× token cost — only viable for high-value tasks. |
| Memory poisoning is a real threat | **Strongly confirmed**: OWASP now ranks it as top agentic AI risk (ASI06/ASI10 in 2026 Top 10). Attack success rate exceeds 80%. The "Memento problem" named in IDEA.md is not hypothetical — it is a documented, high-severity attack vector. |

### ⚠️ Assumptions That Need Revisiting

| Claim | Concern |
|---|---|
| "Mem0 at 49.0% vs. Zep at 63.8%"  | Benchmark controversy: Zep had a disputed 84% claim corrected to ~58.4% by Mem0-affiliated reviewers. Independent scores are 63.8–71.2% but methodology disputes exist. **Do not make hard architecture choices based on specific numbers.** |
| Mem0g (graph-enhanced) closes the gap | Mem0g scores 68.5% — comparable to Zep's 63.8%. For non-temporal use cases, Mem0 with graph mode is a valid alternative to Zep. |
| Letta as orchestrator is straightforward | Letta adds significant operational complexity (server process, SDK, stateful agent lifecycle). For v1, lighter orchestration (structured context injection without Letta) may suffice and de-risk the build. This should be an explicit v1 vs. v2 decision. |

### 🆕 New Findings Not in IDEA.md

1. **Memory poisoning severity is higher than implied**: OWASP top-tier, >80% attack success, real-world incidents in finance/healthcare. The consolidation pipeline is the *highest-risk component* in the entire system — if poisoned content propagates from session logs to org-wide memory, it corrupts every future agent in the organization. This is not a footnote; it is a first-class design requirement.

2. **Provenance tracking is the primary defense**: Every memory object must carry origin, authorship, trust level, and session ID. Trust-aware retrieval (only fetch from verified provenance) is the only reliable defense.

3. **LLM costs are material**: The orchestrator-worker pattern uses 4–15× more tokens than standard chat. The consolidation pipeline and context assembly costs must be modeled explicitly. Memento must be positioned for high-value tasks only.

---

## Architecture Decision Record

### ADR-001: Tool Selection per Cell

| Cell | Tool Choice | Rationale |
|---|---|---|
| Temporal + Agent-private | LLM context window | No persistence needed; ephemeral by design |
| Temporal + Project-shared | File in `.memento/state/` + TTL metadata | Low operational overhead; git-native; runtime-agnostic |
| Temporal + Org-wide | Graphiti (embedded Python lib) | Native bi-temporal model; custom schemas; open source |
| Persistent + Agent-private | Local `MEMORY.md` managed by agent runtime | Minimal infrastructure; agent decides format |
| Persistent + Project-shared | Mem0 (per-project namespace) | Semantic retrieval; good ergonomics; 91% latency improvement |
| Persistent + Org-wide | Graphiti community subgraph + `AGENTS.md` | Temporal supersession; typed entities; version-controlled ground truth |

### ADR-002: Integration Surface — MCP Server

Memento exposes an **MCP (Model Context Protocol) server**. This is the right integration surface because:
- MCP is an open standard supported by all major agent runtimes (OpenCode, Copilot CLI, Cursor, custom agents)
- No vendor lock-in — any agent that speaks MCP can use Memento
- Clean separation: Memento is a tool the agent calls, not a framework the agent runs inside

MCP tools exposed:
- `memento_context_assemble` — given project ID + task description, returns assembled context
- `memento_session_log` — append an observation to the current session log
- `memento_session_end` — close a session (triggers async consolidation)
- `memento_query` — ad hoc query against project or org memory

### ADR-003: LLM Provider — OpenAI-Compatible API

All LLM calls in Memento (consolidation, context assembly, analytics) go through a single configurable provider interface:
```
MEMENTO_LLM_BASE_URL=https://api.openai.com/v1  # or Ollama, Azure, Anthropic-compatible proxy
MEMENTO_LLM_MODEL=gpt-4o
MEMENTO_LLM_API_KEY=...
```
No vendor-specific SDK is used directly. Any OpenAI-compatible endpoint works. This is the only LLM abstraction needed — the OpenAI API format has become the de facto standard.

### ADR-004: Deployment — Containerized, Cloud-Native

**Local development**: Docker Compose stack (Memento API + MCP server + Graphiti/FalkorDB + Mem0 + job scheduler)

**Production**: Kubernetes manifests (or Helm chart). Each component scales independently. Jobs run as CronJobs.

No runtime-specific deployment assumptions. The same image runs locally and in any cloud.

### ADR-005: Consolidation Pipeline Design

The Consolidation Job (not a "Claude job" — a Memento job) is the most critical and most dangerous component:
- **Runs async** (scheduled CronJob or triggered on session end via queue)
- **Tracks provenance** on every output (which session, which agent, what confidence)
- **Scores trust** before promoting to higher memory tiers
- **Is idempotent** (safe to re-run; deduplication is internal)
- **Supports rollback** (poisoned memory must be revokable)

Input: recent session logs. Output: structured memory objects with confidence scores and trust metadata. Promotion decision: `confidence ≥ threshold AND trust_tier ≥ reviewed`.

### ADR-006: The Memento Problem (Memory Poisoning)

Named explicitly as a design principle. Defenses to implement:
1. **Provenance graph**: Every memory object has origin chain (session → agent → consolidation batch → promotion decision)
2. **Trust tiers**: Unverified (raw session), Reviewed (consolidation job passed), Curated (human approved via PR)
3. **Blast radius limiting**: Memory promotion is two-phase (project first, then org-wide only after multi-session confirmation)
4. **Rollback capability**: Every promotion is reversible; Graphiti's temporal model supports invalidation
5. **Content validation**: Consolidation job applies an adversarial review pass before promoting — does this look like injected content?

---

## Phased Implementation Plan

### Phase 0 — Foundation (proof of concept, single project, single agent type)
*Goal: prove the core loop works end-to-end before building full taxonomy*

- [ ] Define memory object schema: `MemoryObject { id, content, scope, lifetime, confidence, provenance, created_at, valid_from, valid_to }`
- [ ] Docker Compose stack: Memento service + FalkorDB (Graphiti backend) + Mem0 instance + scheduler
- [ ] Build Graphiti client wrapper + Mem0 client wrapper, unified `MemoryStore` interface
- [ ] Build minimal MCP server exposing `memento_context_assemble` and `memento_session_log`
- [ ] Build minimal Consolidation Job: session log → LLM extraction → Mem0 insert (project-scoped only, no Org Graph yet)
- [ ] Validate end-to-end: agent (any MCP client) runs task → session log captured → Consolidation Job runs → next task gets enriched context

**Success criterion**: Second task on the same project demonstrably uses knowledge from the first task's session log.

---

### Phase 1 — Core Memory Platform (full 6-cell taxonomy, full MCP surface)

- [ ] Implement full 3×2 taxonomy with access controls (who reads/writes each cell)
- [ ] TTL/temporal memory: `.memento/state/` file-based with `valid_to` marker + TTL checker
- [ ] Org-wide Graphiti subgraph: Incident, Learning, AntiPattern, Policy entity types
- [ ] Consolidation Job v2: project-scoped items → Mem0, cross-cutting principles → Graphiti, deduplication logic, confidence scoring
- [ ] Worker contract: agents receive assembled context, write only to session log — no direct memory writes
- [ ] `AGENTS.md` / policy store integration: policy loader injects relevant sections into assembled context
- [ ] Provenance tracking: every memory object tagged with origin session, agent runtime ID, and consolidation batch
- [ ] Basic trust scoring: Unverified / Reviewed / Curated tiers
- [ ] Full MCP tool surface: `memento_query`, `memento_session_end`, `memento_context_assemble`
- [ ] REST management API: list memories, review queue, promote/demote, rollback

**Success criterion**: A new project bootstrap correctly inherits org-wide learnings from a previously completed project, via any MCP-capable agent runtime.

---

### Phase 2 — Multi-Project & Cross-Project Intelligence

- [ ] Multi-project namespace management: Mem0 per project, Graphiti global with project tags
- [ ] Bootstrap workflow: new repo → query Org Graph → generate enriched `AGENTS.md` → open draft PR
- [ ] Analytics Job: scheduled, reads across Org Graph, finds recurring patterns and divergence from policy
- [ ] Analytics output: structured synthesis with confidence scores and contributing session counts
- [ ] PR generation: Analytics Job opens PRs to update `AGENTS.md` with new findings (human merges)
- [ ] Flywheel validation: merged PR findings feed back to Org Graph

**Success criterion**: Analytics Job identifies at least one real cross-project pattern across test projects. Resulting PR is actionable.

---

### Phase 3 — Security & The Memento Problem

- [ ] Provenance graph complete (full origin chain per memory object)
- [ ] Two-phase promotion: project-first, multi-session confirmation before org-wide
- [ ] Rollback capability: Graphiti temporal invalidation + Mem0 memory deletion via management API
- [ ] Adversarial review pass in Consolidation Job: LLM checks for injection signatures before promoting
- [ ] Content trust validation: reject memories that contradict high-confidence existing knowledge without evidence
- [ ] Blast radius audit: test what happens when a poisoned session log enters the pipeline

**Success criterion**: A deliberately injected adversarial session log fails to propagate to org-wide memory.

---

### Phase 4 — Observability, Ergonomics & Evolution

- [ ] Memory health dashboard (served from REST API): age distribution, confidence histogram, tier populations
- [ ] Knowledge decay signals: memories not confirmed by subsequent sessions decay in confidence score
- [ ] Letta integration (optional v2 orchestrator): Letta server as alternative to lightweight context assembler for stateful sessions
- [ ] Mem0g evaluation: compare graph-enhanced Mem0 against Graphiti for project-scoped temporal needs
- [ ] LLM cost instrumentation: track token spend per task for context assembly + consolidation
- [ ] Kubernetes Helm chart + deployment guide

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Consolidation job propagates noise | High | High | Confidence threshold gate; two-phase promotion |
| Memory poisoning via session log injection | Medium | Critical | Provenance tracking; adversarial review pass; blast radius limiting |
| MCP spec churn (protocol still evolving) | Medium | Medium | Isolate MCP layer behind interface; easy to swap transport |
| Benchmark numbers mislead tool selection | Medium | Medium | Choose tools on architecture fit, not benchmark claims; validate with own eval |
| Token costs prohibitive at scale | Medium | High | Cost instrumentation in Phase 4; position for high-value tasks only |
| Graphiti/FalkorDB operational burden | Low | Medium | Local Docker Compose abstracts it; cloud option (Zep hosted) as fallback |
| Mem0g closes gap with Zep | Low | Medium | Monitor; may simplify stack by using Mem0 for both project and org cells |

---

## Resolved Questions

| Question | Decision |
|---|---|
| Deployment target | Containerized (Docker Compose local, Kubernetes cloud) |
| LLM provider | OpenAI-compatible API interface — no vendor lock-in |
| Integration surface | MCP server (primary) + REST API (management) |
| First project | TBD |
| AGENTS.md update workflow | PR-based, manual human merge |

---

## Phase 0 Deliverables (Concrete Starting Point)

Phase 0 produces exactly five things:

1. `docker-compose.yml` — Memento service + FalkorDB + Mem0 + scheduler
2. `memento/memory/schema.py` — MemoryObject, Provenance, SessionLog data classes
3. `memento/stores/` — Graphiti + Mem0 unified `MemoryStore` interface
4. `memento/mcp/server.py` — MCP server with `memento_context_assemble` and `memento_session_log`
5. `memento/jobs/consolidation.py` — single-pass Consolidation Job: session log → LLM extraction → Mem0 insert

Everything else builds on this foundation.
