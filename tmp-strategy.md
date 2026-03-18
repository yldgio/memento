2. Work in task batches, not all 13 at once

   - Batch A (1 session): P0-T01 scaffold → P0-T02 config → P0-T03 data model → P0-T04 docker compose — these are foundational and sequential/simple
   - Batch B (1 session): P0-T05 graphiti + P0-T06 mem0 + P0-T07 session store — parallel, each is Medium risk
   - Batch C (1 session): P0-T08 MCP server — this is the convergence point, Large/🔴 (public API surface)
   - Batch D (1 session): P0-T09 consolidation + P0-T10 scheduler — highest risk, needs full context
   - Batch E (1 session): P0-T11 health + P0-T12 integration tests + P0-T13 e2e

  3. Model choice

   - Opus
    4.6 for the MCP server (T08) and consolidation pipeline (T09) — these are the hardest tasks with the most design decisions
   - Sonnet
    4.5/4.6 for scaffold, config, data model, docker — straightforward tasks where speed matters more than depth
   - Adversarial reviewers: keep the triple-model spread (GPT-5.3 + Gemini 3 + Opus) for 🔴 tasks, single reviewer for 🟡


"Read @docs/tasks.md and implement Batch A: P0-T01 through P0-T04" — it has everything needed