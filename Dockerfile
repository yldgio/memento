# =============================================================================
# Memento — Agent Memory Platform
# Multi-stage Dockerfile: deps → app
# =============================================================================

# --------------- Stage 1: Builder ---------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN pip install --no-cache-dir hatchling

# Copy only dependency-related files first for layer caching
COPY pyproject.toml README.md ./
COPY memento/__init__.py memento/__init__.py

# Install Python dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .

# --------------- Stage 2: Runtime ---------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY memento/ memento/

# Non-root user for security
RUN useradd --create-home --shell /bin/bash memento
USER memento

# Default command: start the API server
CMD ["python", "-m", "memento.main"]

# Expose REST API and MCP server ports
EXPOSE 8080 8081
