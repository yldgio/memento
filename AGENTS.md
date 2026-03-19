# AGENTS.md

## Project overview

- Project Memento is an **Agent Memory Platform (AMP)** for AI coding agents.
- The codebase is early-stage Python infrastructure focused on memory models, configuration, API scaffolding, and containerized local development.
- Product intent lives in `README.md` and `IDEA.md`; detailed requirements and planned work live in `docs/TRD.md` and `docs/tasks.md`.

## Stack and runtime

- Python `>=3.11`, packaged with `hatchling` (`pyproject.toml`).
- Core libraries: `fastapi`, `uvicorn`, `mcp`, `graphiti-core`, `mem0ai`, `aiosqlite`, `pydantic`, `pydantic-settings`, `httpx`.
- Tooling: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
- Container/dev runtime: `Dockerfile`, `docker-compose.yml`, `.env.example`.

## Important paths

- `memento/` — application package.
- `memento/main.py` — FastAPI entrypoint.
- `memento/config.py` — `MEMENTO_*` environment-driven settings.
- `memento/memory/schema.py` — core enums and Pydantic models for the memory platform.
- `memento/stores/` — shared store protocol plus Graphiti, Mem0, and SQLite session-store backends.
- `memento/mcp/` — MCP server package; `server.py` exposes tools via `mcp.server.fastmcp.FastMCP`.
- `tests/unit/` — unit coverage for config, schema, store behavior, and MCP tool logic.
- `tests/integration/` — integration coverage for backend adapters when external/runtime dependencies are available.
- `docs/TRD.md` — technical requirements source of truth.
- `docs/tasks.md` — implementation backlog and sequencing.
- `.github/agents/` — Copilot-specific agent definitions.
- `.agents/skills/` — repo-local reusable skills.
- `opencode.json` — OpenCode agent/skill configuration.

## Working rules

- Keep `AGENTS.md` itself updated whenever the repo changes in ways that affect structure, commands, workflows, conventions, or architecture.
- Always update documentation and keep it aligned with development; when behavior, architecture, setup, or workflows change, update the relevant docs in the same change.
- Prefer small, surgical changes that match existing patterns.
- Do not invent frameworks, services, or workflows that are not present in the repo.
- If implementation details conflict with docs, fix the code or docs so they agree; do not leave them drifting.

## Code conventions

- Follow existing Python style: type hints everywhere, concise docstrings, Pydantic models for structured data.
- Preserve strict typing; `mypy` is configured in `strict` mode.
- Keep Ruff-compatible formatting and imports; line length is `100`.
- Reuse existing settings and models instead of duplicating config or schema logic.
- Treat secrets as secret values, not plain logged strings.
- When changing env-driven settings behavior, keep `.env.example`, tests, and runtime wiring aligned.

## Validation expectations

- Run focused validation for the area you changed, and prefer existing project commands.
- Main commands:
  - `pytest`
  - `ruff check memento tests`
  - `mypy memento`
  - `docker compose up --build`
- For config or schema changes, update/add unit tests in `tests/unit/`.

## Repository-specific guidance

- `memento/config.py` is the central settings contract; avoid ad hoc environment parsing elsewhere.
- `memento/memory/schema.py` reflects the domain model from `docs/TRD.md`; keep those aligned.
- The current app surface includes the API entrypoint, scheduler stub, config, schema, Batch B store adapters, and the MCP server (Batch C); consolidation job and later orchestration layers are still planned work.
- Use `docs/tasks.md` to understand delivery order before adding new subsystems.

## Agent workflow

- Research the repo before making structural changes.
- When adding new conventions or project rules, reflect them here if future agents will need them.
- If you add or change agent-facing assets (`AGENTS.md`, `.agents/skills/`, `.github/agents/`, `opencode.json`), keep the cross-tool setup coherent.
