FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Build arguments
ARG AGENT_NAME
ARG RELEASE_VERSION="main"

# Validate AGENT_NAME is provided
RUN if [ -z "$AGENT_NAME" ]; then \
        echo "ERROR: AGENT_NAME build argument is required"; \
        echo "Usage: docker build --build-arg AGENT_NAME=<agent> -t exgentic-a2a-<agent> ."; \
        exit 1; \
    fi

# Install system dependencies (git and git-lfs are needed for cloning)
RUN apt-get update && apt-get install -y \
    git \
    git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install

# Configure git to handle large repositories and avoid HTTP/2 issues
RUN git config --global http.postBuffer 524288000 && \
    git config --global http.version HTTP/1.1 && \
    git config --global core.compression 0

# Set working directory
WORKDIR /app

# Clone the Exgentic repository
RUN git clone https://github.com/Exgentic/exgentic.git /app/exgentic

# Checkout the feature/mcp-command branch
WORKDIR /app/exgentic
RUN git checkout feature/mcp-command

# Set UV_SYSTEM_PYTHON before any uv operations to ensure uv uses system Python
ENV UV_SYSTEM_PYTHON=1

# Install Exgentic and its dependencies using uv
RUN uv pip install --system --no-cache -e .

# Copy entrypoint script
WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set environment variables
ENV AGENT_NAME=${AGENT_NAME} \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=INFO \
    PRODUCTION_MODE=True \
    RELEASE_VERSION=${RELEASE_VERSION}

# Create non-root user and set permissions
RUN useradd -m -u 1001 exgentic && \
    chown -R 1001:0 /app && \
    chmod -R g+rwX /app

USER 1001

# Set HOME environment variable to /app so the venv lands where the runtime
# looks for it (exgentic uses Path.home()/".exgentic").
ENV HOME=/app

# Pre-install the agent venv AS THE FINAL USER so it lands at /app/.exgentic and
# is reused at runtime. Without this, the first A2A request reinstalls the venv
# from pypi, which breaks when egress is restricted (e.g. IBAC-enabled pods).
RUN exgentic install --agent ${AGENT_NAME}


# Expose the default port
EXPOSE 8000
# Set the entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]