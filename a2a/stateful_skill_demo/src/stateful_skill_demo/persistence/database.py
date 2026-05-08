import logging
from urllib.parse import urlparse, urlunparse

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    context_id      TEXT PRIMARY KEY,
    current_goal_id TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_turns (
    id          TEXT PRIMARY KEY,
    context_id  TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id     TEXT,
    turn_index  INTEGER NOT NULL,
    text        TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (context_id, turn_index)
);
CREATE INDEX IF NOT EXISTS user_turns_context_received
    ON user_turns (context_id, received_at);

CREATE TABLE IF NOT EXISTS goals (
    id                  TEXT PRIMARY KEY,
    context_id          TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_index          INTEGER NOT NULL,
    user_goal           TEXT NOT NULL,
    originating_turn_id TEXT REFERENCES user_turns(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    current_step_id     TEXT,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (context_id, goal_index)
);
CREATE INDEX IF NOT EXISTS goals_context_status ON goals (context_id, status);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_turns_goal_fk'
    ) THEN
        ALTER TABLE user_turns
            ADD CONSTRAINT user_turns_goal_fk
            FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE SET NULL;
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS plan_steps (
    id                     TEXT PRIMARY KEY,
    context_id             TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id                TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    step_order             INTEGER NOT NULL,
    description            TEXT NOT NULL,
    inputs_json            JSONB NOT NULL DEFAULT '{}',
    expected_result        TEXT NOT NULL DEFAULT '',
    expected_artifact_name TEXT,
    status                 TEXT NOT NULL DEFAULT 'PENDING',
    retry_count            INTEGER NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plan_steps_goal_order ON plan_steps (goal_id, step_order);

CREATE TABLE IF NOT EXISTS artifacts (
    id             TEXT PRIMARY KEY,
    context_id     TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id        TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    step_id        TEXT REFERENCES plan_steps(id) ON DELETE SET NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    value          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    version        INTEGER NOT NULL DEFAULT 1,
    is_stale       BOOLEAN NOT NULL DEFAULT false,
    superseded_by  TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (context_id, name, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS artifacts_current
    ON artifacts (context_id, name)
    WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS execution_logs (
    id                TEXT PRIMARY KEY,
    context_id        TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id           TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    step_id           TEXT NOT NULL REFERENCES plan_steps(id),
    artifact_id       TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    skill_name        TEXT NOT NULL DEFAULT '',
    skill_inputs_json JSONB NOT NULL DEFAULT '{}',
    raw_output        TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    error_json        JSONB,
    runtime_ms        INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS execution_logs_step_created
    ON execution_logs (context_id, step_id, created_at DESC);

CREATE TABLE IF NOT EXISTS review_logs (
    id                  TEXT PRIMARY KEY,
    context_id          TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id             TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    step_id             TEXT NOT NULL REFERENCES plan_steps(id),
    review_status       TEXT NOT NULL,
    goal_achieved       BOOLEAN NOT NULL DEFAULT false,
    reason              TEXT NOT NULL DEFAULT '',
    recommended_action  TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS planner_logs (
    id                  TEXT PRIMARY KEY,
    context_id          TEXT NOT NULL REFERENCES sessions(context_id) ON DELETE CASCADE,
    goal_id             TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    input_context       JSONB NOT NULL DEFAULT '{}',
    generated_plan_json JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Canonical table name used to detect whether the schema has been created.
# `sessions` is the root of the FK graph — if it's missing, everything is.
_SCHEMA_PROBE_TABLE = "sessions"


class Database:
    def __init__(self, dsn: str, *, auto_create: bool = True):
        self._dsn = dsn
        self._auto_create = auto_create
        self._pool: asyncpg.Pool | None = None

    async def _ensure_database_exists(self) -> None:
        parsed = urlparse(self._dsn)
        db_name = parsed.path.lstrip("/")
        if not db_name:
            return

        maintenance_dsn = urlunparse(parsed._replace(path="/postgres"))

        conn = await asyncpg.connect(maintenance_dsn)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
                logger.info("Created database '%s'", db_name)
        finally:
            await conn.close()

    async def init(self) -> None:
        if self._auto_create:
            await self._ensure_database_exists()
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT to_regclass($1)", f"public.{_SCHEMA_PROBE_TABLE}"
            )
            if exists is None:
                await conn.execute(SCHEMA_DDL)
                logger.info("Database schema created")
            else:
                logger.info("Database schema already present; skipping create")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized — call init() first")
        return self._pool
