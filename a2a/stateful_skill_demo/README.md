# Stateful Skill Demo Agent

An [A2A](https://a2a-protocol.org/latest/) agent that implements a multi-stage,
skill-driven planner/executor/reviewer loop on top of
[AG2](https://docs.ag2.ai/) (AutoGen 2) native skills, with PostgreSQL
persistence for multi-turn, multi-goal conversations.

## Overview

The agent takes a user instruction and an A2A `contextId`, and drives the
instruction to completion through a Planner → Executor → Reviewer loop:

- The **Planner** produces exactly one next executable step given the goal,
  prior steps, error/feedback from the last failure, and a bounded summary of
  earlier goals + named artifacts in this conversation.
- The **Executor** runs that one step, optionally using skills scoped to a
  configurable `SKILLS_DIR`.
- The **Reviewer** classifies the step as `SUCCESS`, `PARTIAL_SUCCESS`, or
  `FAILURE`, and when the step failed produces a concrete `recommended_action`
  the Planner consumes on the next iteration.
- A fourth **Responder** agent produces a user-facing reply on zero-step
  completions (when the Planner decides the current goal is already answered
  by prior conversation context).

Every stage is persisted to PostgreSQL before the next stage runs, which is
what makes this more than a one-shot LLM call:

- **Replan on failure** — a failed step does not abort; the Reviewer's
  feedback and the Executor's structured error are fed back into the Planner,
  which chooses to retry with changes, insert a repair step, or abort.
- **Persistent state and session resume** — the loop can be interrupted at any
  point; on the next turn the resume logic decides whether to re-run the
  planner, executor, or reviewer based on the last persisted stage.
- **Multi-goal conversations with artifact reuse** — one `contextId` can hold
  many goals. Successful steps can publish named artifacts (text, JSON, URL,
  or file path) that later goals reference inside step inputs via
  `@{name}` substitution.




## High-level design

```
          A2A client (streaming or request/response)
                         │
                         ▼
          ┌──────────────────────────────┐
          │  A2AStarletteApplication     │  a2a-sdk
          │  DefaultRequestHandler       │
          │  InMemoryTaskStore           │
          └──────────────┬───────────────┘
                         │  execute(context, event_queue)
                         ▼
          ┌──────────────────────────────┐
          │  SkillDemoExecutor           │  per-contextId asyncio.Lock
          │  (a2a_server/server.py)      │  → event_callback, error_callback
          └──────────────┬───────────────┘
                         │  run_turn(context_id, turn_id, user_text, cb)
                         ▼
          ┌──────────────────────────────┐
          │  OrchestrationRunner         │  resolve/create goal
          │  (orchestration/runner.py)   │  resume → plan → exec → review loop
          └──────┬──────────┬──────┬─────┘
                 │          │      │
                 ▼          ▼      ▼
             Planner    Executor  Reviewer   (+ Responder for zero-step done)
             (LLM)      (LLM +    (LLM)
                        AG2 tools)
                 │          │      │
                 └──────────┴──────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │  SessionStore / asyncpg      │  sessions, user_turns, goals,
          │  (persistence/*.py)          │  plan_steps, execution_logs,
          └──────────────────────────────┘  review_logs, planner_logs, artifacts
```

One turn flows as: the server writes a `user_turns` row and calls
`run_turn`; the runner resolves-or-creates the current goal, optionally
resumes a mid-cycle stage, then loops Planner → Executor → Reviewer,
persisting after each stage, until the goal completes, is aborted by the
planner, exceeds the failure budget, or is superseded by a new inbound turn.

### Sessions, goals, user turns, and the A2A `contextId`

The A2A protocol defines a top-level `contextId` on every `Message` that
clients reuse across requests to stick with the same conversation. It is
*the* standard handle for conversation continuity in A2A — any compliant
client (the Mesop demo UI, `curl`, another A2A agent) populates it the same
way, and any compliant agent is free to interpret it however it wants.

This agent interprets `contextId` as the primary key of a long-lived
**session**: one row in `sessions`, created on the first inbound turn with
that id and reused verbatim on every subsequent turn. Everything else in
the schema hangs off that row:

```
 A2A Message.contextId  ──────────►  sessions.context_id   (PK)
                                         │ 1
                                         │
                                         ├─────────────── n ── user_turns
                                         │                       (turn_index ordered,
                                         │                        goal_id optional)
                                         │
                                         └─────────────── n ── goals
                                                                 (goal_index ordered,
                                                                  status ACTIVE / COMPLETED /
                                                                  FAILED / SUPERSEDED)
                                                                    │ 1
                                                                    │
                                                                    └── n ── plan_steps
                                                                               │
                                                                               ├── execution_logs
                                                                               ├── review_logs
                                                                               └── artifacts
```

- One `contextId` → one `sessions` row → many `user_turns` and many `goals`.
- A `user_turn` may start a new goal or append to an existing `ACTIVE` one
  (its nullable `goal_id` FK records which goal actually picked it up).
- A `goal` owns all the plan steps, execution logs, review logs, and
  artifacts produced while pursuing it, so follow-up goals under the same
  `contextId` can reference prior artifacts by name via `@{name}`.

Because the A2A spec keeps `contextId` opaque to the client, resume "just
works" at the protocol level: a client that sends the same `contextId` a
week later lands on the same `sessions` row and picks up with full prior
history. The agent's own resume logic (`orchestration/resume.py`) then
decides whether to re-run the planner, executor, or reviewer based on the
last persisted stage of the active goal.

See [`DESIGN.md`](./DESIGN.md) for the deep dive on schema, context
assembly, artifact substitution, and the resume algorithm.

## Getting started

This agent is designed to run in a Kubernetes cluster. Install it either
through the Kagenti UI (recommended) or by applying the deployment YAMLs
directly.

### Prerequisites

- A Kubernetes cluster with the Kagenti platform installed
- A reachable **OpenAI-compatible LLM endpoint** (OpenAI, an internal LiteLLM
  gateway, a vLLM server, etc.) and an API key stored in a Kubernetes secret
- The agent container image available to the cluster, either pushed to a
  registry or loaded into a local Kind cluster via
  [`build-and-load.sh`](./build-and-load.sh)

### Install via the Kagenti UI

Register this repo and import `a2a/stateful_skill_demo` as an agent. The UI
walks you through creating the necessary secrets (LLM API key, Postgres
credentials) and sets the environment variables listed in
[Configuration](#configuration) on the resulting Deployment.

**Postgres is not deployed by the UI.** The agent depends on a PostgreSQL
instance that must be created separately from the bundled
[`postgres-deployment.yaml`](./postgres-deployment.yaml) before the agent
starts. Apply it first and in the same namespace the UI will deploy into:

```sh
kubectl apply -f a2a/stateful_skill_demo/postgres-deployment.yaml
```

The agent's Deployment references the `skill-demo-postgres` Service and
Secret created by that manifest.

### Install via deployment YAMLs

Apply the bundled manifests directly. Postgres must be up before the agent
starts so the schema can be created on first boot:

```sh
cd a2a/stateful_skill_demo

# 1. Postgres (StatefulSet + Service + Secret for the agent to consume)
kubectl apply -f postgres-deployment.yaml

# 2. Create the LLM API key secret referenced by the agent (name it
#    openai-secret or edit deployment.yaml to match what you have)
kubectl create secret generic openai-secret \
  --namespace team1 \
  --from-literal=apikey='sk-...'

# 3. Agent Deployment + Service
kubectl apply -f deployment.yaml
```

The agent reads configuration from environment variables set in
`deployment.yaml` — see the [Configuration](#configuration) table for what
each one controls. Adjust `LLM_API_BASE`, `LLM_MODEL`, and the secret/image
references in `deployment.yaml` to match your environment.

### Configuration

All configuration is via environment variables (pydantic-settings). When
installing via the Kagenti UI these are set through the UI; when applying
the YAMLs directly they're set in the `env:` block of `deployment.yaml`.
Names and defaults come from `src/stateful_skill_demo/config/settings.py`:

| Variable | Default | Description |
|---|---|---|
| `LLM_API_BASE` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | `dummy` | API key (placeholders accepted for local/cluster LLMs) |
| `LLM_MODEL` | `llama3.1` | Model identifier |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/skill_demo` | PostgreSQL DSN |
| `DATABASE_AUTO_CREATE` | `true` | Create the database (if missing) on startup; schema tables are always created idempotently and never dropped |
| `SKILLS_DIR` | `.skills` | Directory passed to AG2 toolkits |
| `MAX_STEP_RETRIES` | `3` | Per-step retry cap |
| `MAX_TOTAL_FAILURES` | `10` | Per-goal failure budget |
| `PLANNER_PRIOR_GOALS_N` | unset (unlimited) | Cap on prior goals included in planner context |
| `PLANNER_CONTEXT_CHAR_BUDGET` | `8000` | Planner prior-context char budget |
| `HOST` | `0.0.0.0` | Bind host |
| `PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `INFO` | Python log level |
| `AGENT_ENDPOINT` | `http://{HOST}:{PORT}` | URL advertised on the A2A `AgentCard` |

Preset [`.env.openai`](./.env.openai) and [`.env.ollama`](./.env.ollama)
files are provided for convenience when importing the agent through the
Kagenti UI — point the UI at one of them and adjust the values for your
environment.

### Skills

Skills are delivered to the agent as an **OCI image volume** mounted
read-only at `/skills`. The bundled `deployment.yaml` declares it on the
Pod spec:

```yaml
volumes:
  - name: skill
    image:
      reference: quay.io/skillimage/tools/git-assistant:0.2.0-draft
      pullPolicy: IfNotPresent
```

and mounts it into the container at `/skills`, with `SKILLS_DIR=/skills`
telling the agent where to look. **Every skill found under `/skills` is
loaded automatically** — there is no allow-list. To add or change the
skills the agent can use, either:

- point the `volumes[].image.reference` at a different OCI image
  containing the skills you want, or
- use a volume composed of multiple skill images (e.g. one per skill) and
  let the agent pick them all up from `/skills`.

This requires the Kubernetes image-volume feature (GA in 1.33; available
as a feature gate in 1.31+). If your cluster does not support image
volumes, substitute a ConfigMap, PVC, or initContainer-populated
`emptyDir` that lays files out under `/skills` the same way.

## Usage

The agent speaks A2A JSON-RPC on the root path. The `AgentCard` is served
at `/.well-known/agent-card.json` and advertises `streaming=true`.

### Using a context-preserving UI

Use an A2A UI that persists and re-sends `contextId` across turns — the
Kagenti UI does this. Because every stage of the Planner → Executor →
Reviewer loop is persisted to Postgres before the next stage runs, a UI
that keeps the same `contextId` can:

- **Continue a conversation** across turns — a new turn on the same
  `contextId` starts a new goal if the previous goal is terminal
  (`COMPLETED` / `FAILED` / `SUPERSEDED`), or supersedes the current goal
  if it is still `ACTIVE`. Artifacts published in earlier goals remain
  addressable via `@{name}` in later step inputs, so follow-ups like "now
  summarize that file" compose naturally.
- **Recover from a crash or re-route** — if the agent pod crashes or the
  request lands on a different replica that has no in-memory state for
  this conversation, resending on the same `contextId` resumes from the
  last persisted stage rather than starting over.

UIs that mint a new `contextId` per request (or omit it) will see each
turn as an independent conversation and will not benefit from either of
these properties.

If you are writing your own A2A client (JSON-RPC, gRPC, or otherwise)
rather than using a UI, the same rule applies: persist the `contextId`
returned on the first turn — or the one you supplied — and send it on
`message.contextId` in every subsequent request for that conversation.

## Project structure

```
src/stateful_skill_demo/
├── main.py                       # Entry point — builds Settings + app, runs uvicorn
├── observability.py              # Optional OTel GenAI tracing setup
├── a2a_server/
│   └── server.py                 # AgentCard, SkillDemoExecutor, create_app
├── agents/
│   ├── planner.py                # One-step-at-a-time planner (AG2 beta Agent)
│   ├── executor.py               # AG2 beta Agent wired to skill toolkits
│   ├── reviewer.py               # Classifies step results + feedback
│   └── responder.py              # User-facing reply for zero-step completions
├── orchestration/
│   ├── runner.py                 # OrchestrationRunner.run_turn — the main loop
│   ├── resume.py                 # determine_resume_state — mid-cycle resume logic
│   └── context_builder.py        # Bounded prior-goal + artifact context for planner
├── persistence/
│   ├── database.py               # asyncpg pool + schema DDL (drop+create on start)
│   ├── session_store.py          # CRUD for sessions, turns, goals, steps, logs, artifacts
│   └── models.py                 # Internal row helpers
├── schemas/
│   ├── session.py                # Status enums (GoalStatus, StepStatus, ReviewStatus, ...)
│   ├── user_turn.py              # UserTurn model
│   ├── goal.py                   # Goal model
│   ├── plan_step.py              # PlanStep model
│   ├── execution_result.py       # ExecutionResult, ReviewResult, PlannerLog
│   └── artifact.py               # Artifact model + ArtifactKind enum
└── config/
    └── settings.py               # pydantic-settings Settings class
```

## Testing

Tests live in the repo-root `tests/` directory, per the parent
[`CLAUDE.md`](../../CLAUDE.md). They mock heavy dependencies (`langchain`,
`opentelemetry`, `fastmcp`) so they run without installing every agent's
dependencies.

```sh
# From the repo root
python -m pytest tests/a2a/test_stateful_skill_demo.py -v

# Or run all A2A agent tests
python -m pytest tests/a2a/ -v
```

Notable: `tests/a2a/test_stateful_skill_demo.py::TestRunTurnEmissionContract`
is an AST-based test that asserts every `return` in `run_turn` has a
preceding `emit(..., final=True)` — enforcing the A2A event emission
contract.

## License / Attribution

This agent is part of the [kagenti/agent-examples](https://github.com/kagenti/agent-examples)
repository — community examples of A2A agents and MCP tools for the
[Kagenti](https://github.com/kagenti) platform. See the repository root for
license and contribution guidelines (DCO sign-off is required on all
commits).
