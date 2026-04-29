import logging

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    context_id      TEXT PRIMARY KEY,
    user_goal       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    current_step_id TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan_steps (
    id              TEXT PRIMARY KEY,
    context_id      TEXT NOT NULL REFERENCES sessions(context_id),
    step_order      INTEGER NOT NULL,
    description     TEXT NOT NULL,
    inputs_json     JSONB NOT NULL DEFAULT '{}',
    expected_result TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'PENDING',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id              TEXT PRIMARY KEY,
    context_id      TEXT NOT NULL REFERENCES sessions(context_id),
    step_id         TEXT NOT NULL REFERENCES plan_steps(id),
    skill_name      TEXT NOT NULL DEFAULT '',
    skill_inputs_json JSONB NOT NULL DEFAULT '{}',
    raw_output      TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    error_json      JSONB,
    runtime_ms      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_logs (
    id                  TEXT PRIMARY KEY,
    context_id          TEXT NOT NULL REFERENCES sessions(context_id),
    step_id             TEXT NOT NULL REFERENCES plan_steps(id),
    review_status       TEXT NOT NULL,
    goal_achieved       BOOLEAN NOT NULL DEFAULT false,
    reason              TEXT NOT NULL DEFAULT '',
    recommended_action  TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS planner_logs (
    id                  TEXT PRIMARY KEY,
    context_id          TEXT NOT NULL REFERENCES sessions(context_id),
    input_context       JSONB NOT NULL DEFAULT '{}',
    generated_plan_json JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_DDL)
        logger.info("Database initialized")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not initialized — call init() first")
        return self._pool
