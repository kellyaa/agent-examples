# Requirements: Kubernetes Debug Agent (A2A)

## 1) Summary

Build a Python agent service that diagnoses root causes of application failures running on Kubernetes. The agent inspects the state of a Kubernetes deployment via MCP tools and retrieves code changes from GitHub to correlate recent changes with the observed failure. It produces a structured root-cause analysis report.

Key constraints:

* **Framework:** AG2 (formerly AutoGen) `>=0.11.2` with built-in A2A serving support
* **Transport:** Serve requests via the A2A (Agent-to-Agent) protocol
* **LLM:** Any OpenAI-compatible endpoint (configurable)
* **Tools:** Discovered at startup from a single MCP server endpoint (Streamable HTTP) that exposes both Kubernetes and GitHub tools under distinct prefixes
* **Observability:** OpenTelemetry tracing of all agent and LLM activity

---

## 2) Goals

1. **Root-cause diagnosis** -- Accept a free-form user query describing a Kubernetes application failure. The agent autonomously determines the namespace, deployment, GitHub repository, and other context it needs from the query and from tool calls.

2. **Multi-agent orchestration** -- Use at least three AG2 `ConversableAgent` instances (Planner, Kubernetes Agent, GitHub Agent) coordinated in a dynamic loop.

3. **Kubernetes inspection** -- Examine deployment status, pod state, events, logs, resource limits, and any other relevant cluster data via `k8s_*` MCP tools.

4. **GitHub change analysis** -- Retrieve recent changes (commits, diffs, releases, tags) from the relevant repository via `github_*` MCP tools and compare what changed since the last release to identify potential contributors to the failure.

5. **Structured output** -- Produce a root-cause analysis report with clearly defined sections (see FR-8).

6. **A2A compliance** -- Expose an A2A server with an Agent Card, streaming status events, and final artifact delivery.

7. **OpenTelemetry tracing** -- Capture traces for LLM calls, agent interactions, and tool invocations, exportable to any OTLP-compatible backend or console.

---

## 3) Non-goals

* Not a general-purpose Kubernetes management tool; scope is limited to failure diagnosis.
* Not implementing custom MCP servers; the agent connects to an externally provided MCP endpoint.
* Not performing remediation actions (no writes/patches to the cluster or repository).
* Not supporting non-OpenAI-compatible LLM APIs in iteration 1.

---

## 4) Stakeholders / Users

* **End user:** Sends a free-form query via an A2A client describing an application failure.
* **Tool provider:** Operates the MCP server exposing Kubernetes and GitHub tools.
* **Operator:** Deploys and configures the agent service, model credentials, MCP endpoint, and tracing.

---

## 5) High-level Architecture

### 5.1 Components

1. **A2A Server Layer**
   - HTTP server hosting A2A endpoints (via A2A Python SDK + AG2 built-in A2A support).
   - Responsible for request parsing, agent lifecycle, streaming progress events, and response formatting.

2. **Agent Core (AG2 Multi-Agent)**
   - **Planner Agent** -- Decides the next diagnostic step. Has no direct tool access. Delegates to the Kubernetes Agent or GitHub Agent as needed, receives results, and iterates until it has enough information to produce a diagnosis.
   - **Kubernetes Agent** -- Has access only to MCP tools prefixed with the configured Kubernetes prefix (default `k8s_`). Inspects cluster state on behalf of the Planner.
   - **GitHub Agent** -- Has access only to MCP tools prefixed with the configured GitHub prefix (default `github_`). Retrieves code changes, releases, and diffs on behalf of the Planner.

3. **MCP Tooling Adapter**
   - Connects to a single MCP server endpoint via Streamable HTTP at startup.
   - Fetches all available tools and partitions them by configured prefix into two toolsets.
   - Registers each toolset with the appropriate agent.

4. **OpenTelemetry Tracing**
   - Initializes a `TracerProvider` with AG2's `instrument_llm_wrapper` and `instrument_agent`.
   - Supports OTLP export (gRPC or HTTP) and console export for development.

### 5.2 Data Flow (happy path)

```
A2A client
  -> A2A Server
    -> Planner Agent (decides next step)
      -> Kubernetes Agent (inspects cluster via k8s_* tools) \
      -> GitHub Agent (retrieves changes via github_* tools)  |-- loop
      -> Planner Agent (analyzes results, decides next step) /
    -> Planner Agent (produces structured RCA report)
  -> A2A client (receives report as artifact)
```

### 5.3 Orchestration Pattern

The Planner operates in a dynamic loop:

1. Planner receives the user query and decides which agent to delegate to first.
2. Planner sends a specific instruction to the chosen agent (Kubernetes or GitHub).
3. The delegated agent executes MCP tool calls and returns findings.
4. Planner analyzes the findings and decides the next step -- either delegate again (to either agent) or conclude.
5. Loop continues until the Planner has sufficient information or the iteration limit is reached.
6. Planner produces the final structured root-cause analysis report.

---

## 6) Configuration

All configuration via environment variables, with `.env` file support.

### 6.1 Required Settings

| Variable | Description | Default |
|---|---|---|
| `MCP_URL` | Streamable HTTP endpoint for the MCP server | *(none -- required)* |
| `LLM_API_KEY` | API key for the OpenAI-compatible LLM endpoint | *(none -- required)* |

### 6.2 Optional Settings

| Variable | Description | Default |
|---|---|---|
| `LLM_MODEL` | Model identifier | `gpt-4` |
| `LLM_API_BASE` | Base URL for the OpenAI-compatible API | `https://api.openai.com/v1` |
| `LLM_TEMPERATURE` | Sampling temperature | `0` |
| `EXTRA_HEADERS` | JSON string of extra headers for the LLM API | `{}` |
| `K8S_TOOL_PREFIX` | Prefix for Kubernetes MCP tools | `k8s_` |
| `GITHUB_TOOL_PREFIX` | Prefix for GitHub MCP tools | `github_` |
| `MAX_PLAN_STEPS` | Maximum planner loop iterations | `15` |
| `SERVICE_PORT` | A2A server port | `8000` |
| `A2A_HOST` | A2A server bind address | `0.0.0.0` |
| `A2A_PUBLIC_URL` | Publicly routable A2A base URL for agent discovery | *(none)* |
| `LOG_LEVEL` | Application log level | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP endpoint for trace export | *(none -- tracing disabled)* |
| `OTEL_CONSOLE_TRACING` | Print traces to console when no OTLP endpoint | `false` |

### 6.3 Config Implementation

- Use `pydantic-settings` (`BaseSettings`) with `.env` file support, consistent with the `slack_researcher` and `simple_generalist` patterns.
- Validate `EXTRA_HEADERS` as JSON.
- Instantiate a module-level `settings` singleton.

---

## 7) Functional Requirements

### 7.1 Startup & Tool Discovery

**FR-1** On startup, connect to the MCP server at `MCP_URL` via Streamable HTTP.

**FR-2** Fetch all available tools and partition them into two sets based on `K8S_TOOL_PREFIX` and `GITHUB_TOOL_PREFIX`. Log the tool names in each set.

**FR-3** Register Kubernetes tools with the Kubernetes Agent (for both LLM visibility and execution). Register GitHub tools with the GitHub Agent likewise. The Planner Agent has no tools registered.

### 7.2 Agent Composition

**FR-4** Create at least three AG2 `ConversableAgent` instances:

- **Planner Agent**: Uses the LLM. System prompt instructs it to act as a diagnostic planner. It receives user queries and findings from other agents. It outputs either a delegation instruction (specifying which agent to use and what to investigate) or the final RCA report.
- **Kubernetes Agent**: Uses the LLM with `k8s_*` tools registered. System prompt instructs it to inspect Kubernetes resources to answer specific questions from the Planner.
- **GitHub Agent**: Uses the LLM with `github_*` tools registered. System prompt instructs it to retrieve and analyze code changes, releases, and diffs to answer specific questions from the Planner.

**FR-5** Create a `UserProxy` agent (no LLM, `human_input_mode="NEVER"`) that executes tool calls on behalf of the Kubernetes and GitHub agents.

### 7.3 Orchestration Loop

**FR-6** Implement a dynamic planner loop:

1. The Planner receives the original user query.
2. The Planner decides the next action: delegate to Kubernetes Agent, delegate to GitHub Agent, or produce the final report.
3. When delegating, use `a_initiate_chat()` with the chosen agent, passing the Planner's specific instruction.
4. Feed the agent's response back to the Planner for the next iteration.
5. Repeat until the Planner produces a final report or `MAX_PLAN_STEPS` is reached.
6. If the iteration limit is reached, the Planner must produce a best-effort report with whatever information has been gathered.

**FR-7** Emit A2A streaming status events at each step of the planner loop (e.g., "Inspecting pod status in namespace X", "Retrieving recent commits from repo Y").

### 7.4 Output

**FR-8** The final output must be a structured root-cause analysis report containing at least these sections:

- **Summary**: One-paragraph overview of the failure and likely root cause.
- **Kubernetes Findings**: What was observed in the cluster (pod states, events, logs, error messages).
- **Code Changes**: What changed since the last release (commits, files modified, relevant diffs).
- **Root Cause**: The most likely cause of the failure, correlating cluster state with code changes.
- **Recommendation**: Suggested next steps to resolve the issue.

**FR-9** Return the report as an A2A artifact with content type `text/markdown`.

### 7.5 A2A Interface

**FR-10** Expose an Agent Card describing the agent's capabilities and skills (Kubernetes failure diagnosis).

**FR-11** Support streaming status updates during execution via A2A task events.

**FR-12** Use the A2A Python SDK (`a2a-sdk`) and AG2's built-in A2A serving capabilities to minimize boilerplate.

### 7.6 OpenTelemetry Tracing

**FR-13** Initialize an OpenTelemetry `TracerProvider` on startup following the pattern in the `simple_generalist` agent:
- If `OTEL_EXPORTER_OTLP_ENDPOINT` is set, export traces via OTLP.
- If `OTEL_CONSOLE_TRACING` is true, export traces to console.
- Instrument AG2 LLM calls via `instrument_llm_wrapper()`.
- Instrument agents via `instrument_agent()`.

---

## 8) Non-Functional Requirements

### 8.1 Reliability & Limits

- Hard iteration limit (`MAX_PLAN_STEPS`) prevents runaway loops.
- Graceful handling when the MCP server is unavailable (fail fast with a clear error artifact).
- Timeouts on individual MCP tool calls.

### 8.2 Security

- No secrets in logs.
- Read-only operations only; no cluster writes, no repository writes.
- Input validation before passing tool arguments.

### 8.3 Observability

- Structured logs per task with step details, tool calls, durations, and errors.
- OpenTelemetry traces for all LLM and tool interactions.
- A2A streaming events for real-time progress visibility.

---

## 9) Project Structure

```
k8s_debug_agent/
  requirements.md
  pyproject.toml
  .env.template
  k8s_debug_agent/
    __init__.py
    config.py          # Pydantic BaseSettings
    agents.py          # AG2 ConversableAgent definitions (Planner, K8s, GitHub, UserProxy)
    main.py            # K8sDebugAgent orchestration (planner loop)
    prompts.py         # System prompts for each agent
    data_types.py      # Pydantic models (RCA report structure, planner decisions)
    event.py           # Event base class for A2A streaming
    llm.py             # LLM config wrapper for AG2
    tracing.py         # OpenTelemetry setup (TracerProvider, instrumentation)
  a2a_agent.py         # A2A server integration & entry point
```

---

## 10) Technology Choices

- **Python** 3.11+
- **AG2** `>=0.11.2` with extras: `openai`, `mcp`, `tracing`
- **OpenTelemetry** (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`)
- **Pydantic / pydantic-settings** for configuration and data models
- **MCP transport:** Streamable HTTP via `autogen.mcp.mcp_client`

---

## 11) Prompting Policy

**PR-1** Planner system prompt must:
- Instruct the agent to act as a Kubernetes failure diagnostician.
- Define the delegation protocol (how to request work from K8s/GitHub agents).
- Define the output format for the final RCA report (sections from FR-8).
- Emphasize correlating Kubernetes state with code changes.

**PR-2** Kubernetes Agent system prompt must:
- Instruct the agent to use only its available tools to inspect cluster resources.
- Return factual findings without speculation.

**PR-3** GitHub Agent system prompt must:
- Instruct the agent to use only its available tools to retrieve repository data.
- Support multiple methods for determining "last release" (tags, GitHub releases, configurable SHA).
- Return factual findings without speculation.

---

## 12) Acceptance Criteria

1. **Boot + discovery** -- Given an MCP server exposing `k8s_*` and `github_*` tools, on startup the agent partitions tools correctly and exposes itself over A2A.

2. **Free-form query** -- Given a query like "My app in namespace production is crash-looping, repo is org/myapp", the agent autonomously determines the namespace, deployment, and repository to investigate.

3. **Multi-step diagnosis** -- The Planner delegates to both the Kubernetes and GitHub agents across multiple iterations, gathering pod status, logs, events, recent commits, and diffs.

4. **Structured RCA report** -- The final artifact is a markdown report containing all sections defined in FR-8.

5. **Iteration limit** -- A query designed to loop indefinitely stops at `MAX_PLAN_STEPS` with a best-effort report.

6. **Tracing** -- When `OTEL_CONSOLE_TRACING=true`, spans for LLM calls and agent interactions are printed to console.

7. **Streaming events** -- An A2A client observes real-time status updates as the agent progresses through its diagnostic steps.

---

## 13) Open Questions (safe defaults)

- **Multiple MCP servers:** Currently designed for a single endpoint with prefixed tools. If separate endpoints are needed in the future, extend `MCP_URL` to support comma-separated URLs with prefix mapping. Default: single endpoint.
- **Concurrent requests:** Default: single-flight (one diagnosis at a time). Queue or reject additional requests.
- **Human-in-the-loop:** Default: no approval required for tool calls (all tools are read-only).
- **Release detection strategy:** The GitHub Agent should attempt tags first, then GitHub releases, then fall back to recent commits. This is prompt-driven, not hard-coded.
