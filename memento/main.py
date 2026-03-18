"""FastAPI application entrypoint for Memento REST API and MCP server."""

from fastapi import FastAPI

app = FastAPI(
    title="Memento",
    description="Agent Memory Platform — LLM-agnostic memory system for AI coding agents",
    version="0.1.0",
)


def main() -> None:
    """Start the Memento API server."""
    import uvicorn

    from memento.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "memento.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
