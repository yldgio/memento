---
name: python-mcp-server
description: Build, refactor, explain, or review Model Context Protocol (MCP) servers in Python using the official Python SDK and FastMCP. Use this whenever the user mentions Python MCP servers, FastMCP, MCP tools/resources/prompts, stdio transport, streamable HTTP transport, MCP server scaffolding, or converting a Python service into an MCP server, even if they do not explicitly say "use a skill."
---

# Python MCP Server

Use this skill to help with Python-based Model Context Protocol servers built with the official `mcp` SDK.

The goal is not just to dump snippets. Guide the work toward a working server shape that matches the user's transport, data model, and runtime constraints.

## When to use

Use this skill when the task involves any of the following:

- creating a new MCP server in Python
- adding or updating MCP tools, resources, or prompts
- converting existing Python logic into MCP endpoints
- choosing between stdio and streamable HTTP transport
- wiring shared resources through lifespan context
- using `Context` for logging, progress, elicitation, or model sampling
- reviewing or debugging Python MCP server code

If the task is not actually about MCP server behavior, do not force this skill.

## Default approach

1. Identify the server shape the user needs.
2. Prefer `FastMCP` unless the task clearly requires a lower-level server.
3. Keep tool functions narrow and strongly typed.
4. Return structured outputs when the result is machine-readable.
5. Use the simplest transport that fits:
   - `mcp.run()` for stdio
   - `mcp.run(transport="streamable-http")` for HTTP
6. Preserve clean separation between:
   - server setup
   - tool/resource/prompt registration
   - shared runtime dependencies

## Core implementation rules

- Use `uv` for project management when you are setting up or documenting the project.
- Install the Python MCP SDK as `mcp`, and use `mcp[cli]` when the CLI tooling is needed.
- Import FastMCP from `mcp.server.fastmcp`.
- Register capabilities with `@mcp.tool()`, `@mcp.resource()`, and `@mcp.prompt()`.
- Always include type hints. They drive schema generation and validation.
- Prefer Pydantic models, TypedDicts, or dataclasses for structured outputs.
- Use async functions for I/O-bound operations.
- Use docstrings to describe tools clearly because they become tool descriptions.

## Transport guidance

### Stdio

Use stdio for local tool integrations and editor/CLI-hosted servers:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("My Server")

if __name__ == "__main__":
    mcp.run()
```

### Streamable HTTP

Use HTTP when the server needs to run as a standalone web service or be hosted behind an HTTP boundary:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("My HTTP Server")

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

For stateless HTTP deployments, consider `stateless_http=True`.

## Tool design guidance

- Keep each tool focused on one job.
- Use descriptive parameter names.
- Validate inputs with typed fields and clear descriptions.
- Prefer explicit failures over silent fallbacks.
- Return structured output when callers benefit from predictable schema.

Example:

```python
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("Weather Server")


class WeatherData(BaseModel):
    temperature: float = Field(description="Temperature in Celsius")
    condition: str
    humidity: float


@mcp.tool()
def get_weather(city: str) -> WeatherData:
    """Get weather for a city."""
    return WeatherData(
        temperature=22.5,
        condition="sunny",
        humidity=65.0,
    )
```

## Resource and prompt patterns

Use resources when the interaction is naturally addressable by URI.

```python
@mcp.resource("users://{user_id}")
def get_user(user_id: str) -> str:
    """Get user profile data."""
    return f"User {user_id} profile data"
```

Use prompts when you want the server to provide reusable prompt construction logic rather than direct tool execution.

## Using Context well

Use `Context` when the task involves progress reporting, logging, user elicitation, or sampling.

```python
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

mcp = FastMCP("Processing Server")


@mcp.tool()
async def process_data(
    data: str,
    ctx: Context[ServerSession, None],
) -> str:
    """Process data with logging."""
    await ctx.info(f"Processing: {data}")
    await ctx.report_progress(0.5, 1.0, "Halfway done")
    return f"Processed: {data}"
```

Available patterns include:

- `await ctx.debug(...)`
- `await ctx.info(...)`
- `await ctx.warning(...)`
- `await ctx.error(...)`
- `await ctx.report_progress(progress, total, message)`
- `await ctx.elicit(message, schema)`
- `await ctx.session.create_message(...)`

## Lifespan and shared dependencies

When tools need shared services such as a database client, cache, or API session, use a lifespan context manager instead of global mutable state.

```python
from contextlib import asynccontextmanager
from dataclasses import dataclass
from mcp.server.fastmcp import Context, FastMCP


@dataclass
class AppContext:
    db: "Database"


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    db = await Database.connect()
    try:
        yield AppContext(db=db)
    finally:
        await db.disconnect()


mcp = FastMCP("My App", lifespan=app_lifespan)


@mcp.tool()
def query(sql: str, ctx: Context) -> str:
    """Query database."""
    db = ctx.request_context.lifespan_context.db
    return db.execute(sql)
```

## HTTP integration notes

If the user already has a Starlette or FastAPI application:

- prefer mounting the MCP app instead of rewriting everything
- use `mcp.run(transport="streamable-http")` for simple standalone servers
- mount multiple MCP servers at different paths when needed
- configure CORS carefully for browser-based clients, including exposing the `Mcp-Session-Id` header

## Operational guidance

- Use environment variables for configuration.
- Log to stderr when stdio transport is involved.
- Clean up resources in lifespan shutdown.
- Keep security in mind when exposing file system access, shell access, or network reachability.
- Enable JSON responses when the client expects modern structured behavior.

## Validation checklist

Before calling the work done, verify:

- imports are correct for the installed SDK version
- tool signatures are fully typed
- chosen transport matches the use case
- structured return types serialize cleanly
- shared dependencies are not leaked or left unclosed
- the server can be started with the documented command

Useful commands:

- `uv run mcp dev server.py`
- `uv run mcp install server.py`

## Error-handling pattern

Prefer narrow, intentional handling that preserves the real failure mode.

```python
@mcp.tool()
async def risky_operation(input: str) -> str:
    """Operation that might fail."""
    try:
        result = await perform_operation(input)
        return f"Success: {result}"
    except Exception as exc:
        return f"Error: {str(exc)}"
```

If you are working in a stricter production codebase, prefer domain-specific exceptions and structured failures instead of broad exception handling.

## What good outputs look like

Good outputs produced with this skill usually include:

- a complete FastMCP server skeleton or targeted patch
- transport choice that is explained by the use case
- typed tools/resources/prompts
- clear structured return models where appropriate
- practical startup or validation commands

Avoid vague MCP advice that is not grounded in the user's Python code or requested transport.
