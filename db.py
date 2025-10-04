# db.py – central async-pg helper for CTFO bot
# ===============================================================
# Last update: 2024-10-08
#
# • generic helpers  fetch_one / fetch_all / execute / close
# • full XP-system schema (xp_members, xp_boosts, …)
# • keeps **all** previously-public methods unchanged
#
# Tips:
#   await db.connect()   → open pool + run migrations
#   await db.close()     → graceful shutdown
# ===============================================================
from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence, Set

import asyncpg


class Database:
    """Thin wrapper around an async-pg pool + convenience helpers."""

    # ───────────────────────────────────────────────────────────
    # INIT / POOL
    # ───────────────────────────────────────────────────────────
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Open pool and run idempotent migrations."""
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self._init_tables()

    async def close(self) -> None:
        """Gracefully close the connection-pool (call on shutdown)."""
        if self.pool and not self.pool.closed:
            await self.pool.close()

    # ───────────────────────────────────────────────────────────
    # GENERIC SMALL HELPERS  (used by newer cogs)
    # ───────────────────────────────────────────────────────────
    async def fetch_one(self, sql: str, *args) -> Dict[str, Any] | None:
        """Return first row as dict or None."""
        async with self.pool.acquire() as conn:       # type: ignore[arg-type]
            row = await conn.fetchrow(sql, *args)
            return dict(row) if row else None

    async def fetch_all(self, sql: str, *args) -> List[Dict[str, Any]]:
        """Return all rows as list[dict]."""
        async with self.pool.acquire() as conn:       # type: ignore[arg-type]
            rows: Sequence[asyncpg.Record] = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]

    async def execute(self, sql: str, *args) -> None:
        """Run statement that does not return rows."""
        async with self.pool.acquire() as conn:       # type: ignore[arg-type]
            await conn.execute(sql, *args)

    # ───────────────────────────────────────────────────────────
    # MIGRATIONS
    # ───────────────────────────────────────────────────────────
    async def _init_tables(self) -> None:
        async with self.pool.acquire() as conn:       # type: ignore[arg-type]
            await conn.execute(
                """
-- ═════════════════════ Core / legacy tables ═════════════════════
CREATE TABLE IF NOT EXISTS codes (
    name   TEXT PRIMARY KEY,
    pin    VARCHAR(4) NOT NULL,
    public BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS reviewers ( user_id BIGINT PRIMARY KEY );

CREATE TABLE IF NOT EXISTS activity (
    user_id BIGINT PRIMARY KEY,
    streak  INTEGER,
    date    DATE,
    warned  BOOLEAN,
    last    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS giveaways (
    id         SERIAL PRIMARY KEY,
    channel_id BIGINT,
    message_id BIGINT,
    prize      TEXT,
    start_ts   BIGINT,
    end_ts     BIGINT,
    active     BOOLEAN,
    note       TEXT
);
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS start_ts BIGINT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS note TEXT;

CREATE TABLE IF NOT EXISTS member_forms (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT,
    created_at TIMESTAMP DEFAULT now(),
    data       JSONB,
    status     TEXT NOT NULL DEFAULT 'pending',
    message_id BIGINT,
    region     TEXT,
    focus      TEXT
);

CREATE TABLE IF NOT EXISTS staff_applications (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT,
    role       TEXT,
    message_id BIGINT,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inactive_members (
    user_id  BIGINT PRIMARY KEY,
    until_ts BIGINT
);

CREATE TABLE IF NOT EXISTS exempt_users ( user_id BIGINT PRIMARY KEY );

CREATE TABLE IF NOT EXISTS activity_audit (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    details TEXT
);

CREATE TABLE IF NOT EXISTS todo_tasks (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT,
    creator_id  BIGINT,
    description TEXT      NOT NULL,
    max_claims  INTEGER   NOT NULL DEFAULT 0,
    claimed     BIGINT[]  NOT NULL DEFAULT '{}',
    message_id  BIGINT,
    completed   BOOLEAN   NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ═════════════════════ Feedback tables ═════════════════════
CREATE TABLE IF NOT EXISTS anon_feedback_cooldown (
    user_id BIGINT PRIMARY KEY,
    last_ts TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id              SERIAL PRIMARY KEY,
    msg_id          BIGINT      NOT NULL,
    author_id       BIGINT      NOT NULL,   -- 0 == anonymous
    category        TEXT,
    target_id       BIGINT,
    text            TEXT,
    rating          INT,
    attachment_urls TEXT[],
    status          TEXT        NOT NULL DEFAULT 'Open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═════════════════════ XP system (NEW) ═════════════════════
CREATE TABLE IF NOT EXISTS xp_members (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    xp       BIGINT  NOT NULL DEFAULT 0,
    level    INTEGER NOT NULL DEFAULT 0,
    last_msg TIMESTAMPTZ,
    streak   INTEGER NOT NULL DEFAULT 0,
    voice_secs BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS xp_members_xp_idx
          ON xp_members (guild_id, xp DESC);

CREATE TABLE IF NOT EXISTS xp_log (
    id       BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    delta    INTEGER NOT NULL,
    reason   TEXT,
    ts       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xp_channel_mult (
    guild_id  BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    mult      NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS xp_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    min_level INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS xp_levelup_channel (
    guild_id   BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS xp_boosts (
    guild_id            BIGINT PRIMARY KEY,
    multiplier          NUMERIC(4,2) NOT NULL,
    ends_at             TIMESTAMPTZ  NOT NULL,
    message             TEXT,
    announce_channel_id BIGINT,
    announce_msg_id     BIGINT
);

CREATE TABLE IF NOT EXISTS xp_voice_excluded (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
"""
            )

    # ═══════════════════ CODES ═══════════════════
    async def get_codes(self, *, only_public: bool = False) -> Dict[str, tuple[str, bool]]:
        q = "SELECT name, pin, public FROM codes"
        if only_public:
            q += " WHERE public=TRUE"
        q += " ORDER BY name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q)
            return {r["name"]: (r["pin"], r["public"]) for r in rows}

    async def add_code(self, name: str, pin: str, public: bool):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO codes (name, pin, public)
                VALUES ($1,$2,$3)
                ON CONFLICT(name) DO UPDATE SET pin=$2, public=$3
                """,
                name,
                pin,
                public,
            )

    async def edit_code(self, name: str, pin: str, public: bool | None = None):
        async with self.pool.acquire() as conn:
            if public is None:
                await conn.execute("UPDATE codes SET pin=$2 WHERE name=$1", name, pin)
            else:
                await conn.execute(
                    "UPDATE codes SET pin=$2, public=$3 WHERE name=$1",
                    name,
                    pin,
                    public,
                )

    async def remove_code(self, name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM codes WHERE name=$1", name)

    # ═══════════════════ REVIEWERS ═══════════════════
    async def get_reviewers(self) -> Set[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM reviewers")
            return {r["user_id"] for r in rows}

    async def add_reviewer(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO reviewers (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                uid,
            )

    async def remove_reviewer(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM reviewers WHERE user_id=$1", uid)

    # ═══════════════════ ACTIVITY ═══════════════════
    async def get_activity(self, uid: int) -> Dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM activity WHERE user_id=$1", uid)
            return dict(row) if row else None

    async def set_activity(self, uid, streak, date_, warned, last):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity (user_id, streak, date, warned, last)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT(user_id) DO UPDATE
                  SET streak=$2, date=$3, warned=$4, last=$5
                """,
                uid,
                streak,
                date_,
                warned,
                last,
            )

    async def get_all_activity(self) -> Dict[int, Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM activity")
            return {r["user_id"]: dict(r) for r in rows}

    # ═══════════════════ INACTIVE MEMBERS ═══════════════════
    async def add_inactive(self, uid: int, until_ts: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO inactive_members (user_id, until_ts)
                VALUES ($1,$2)
                ON CONFLICT(user_id) DO UPDATE SET until_ts=$2
                """,
                uid,
                until_ts,
            )

    async def remove_inactive(self, uid: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM inactive_members WHERE user_id=$1", uid)

    async def get_expired_inactive(self, now_ts: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM inactive_members WHERE until_ts <= $1", now_ts
            )
            return [dict(r) for r in rows]

    # ═══════════════════ MEMBER FORMS ═══════════════════
    async def add_member_form(self, uid, data: dict, message_id: int | None = None):
        async with self.pool.acquire() as conn:
            d = json.loads(json.dumps(data))  # ensure JSON-serialisable
            await conn.execute(
                """
                INSERT INTO member_forms (user_id, data, region, focus, message_id, status)
                VALUES ($1,$2,$3,$4,$5,'pending')
                """,
                uid,
                json.dumps(d),
                d.get("region"),
                d.get("focus"),
                message_id,
            )

    async def update_member_form_status(self, message_id: int, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE member_forms SET status=$1 WHERE message_id=$2",
                status,
                message_id,
            )

    async def get_pending_member_forms(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM member_forms
                WHERE status='pending' AND message_id IS NOT NULL
                """
            )
            return [dict(r) for r in rows]

    # ═══════════════════ STAFF APPLICATIONS ═══════════════════
    async def add_staff_app(self, uid: int, role: str, msg_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO staff_applications (user_id, role, message_id)
                VALUES ($1,$2,$3)
                """,
                uid,
                role,
                msg_id,
            )

    async def update_staff_app_status(self, msg_id: int, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE staff_applications SET status=$1 WHERE message_id=$2",
                status,
                msg_id,
            )

    async def get_pending_staff_apps(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM staff_applications WHERE status='pending'"
            )
            return [dict(r) for r in rows]

    # ═══════════════════ ACTIVITY EXEMPT / AUDIT ═══════════════════
    async def add_exempt_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO exempt_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id,
            )

    async def remove_exempt_user(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM exempt_users WHERE user_id=$1", user_id)

    async def get_exempt_users(self) -> Set[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM exempt_users")
            return {r["user_id"] for r in rows}

    async def log_activity_event(self, user_id: int, event_type: str, details: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO activity_audit (user_id, event_type, details)
                VALUES ($1,$2,$3)
                """,
                user_id,
                event_type,
                details,
            )

    # ═══════════════════ TO-DO LIST ═══════════════════
    async def add_todo(
        self,
        guild_id: int,
        creator_id: int,
        description: str,
        max_claims: int,
        message_id: int,
    ):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO todo_tasks
                  (guild_id, creator_id, description,
                   max_claims, message_id)
                VALUES ($1,$2,$3,$4,$5)
                """,
                guild_id,
                creator_id,
                description,
                max_claims,
                message_id,
            )

    async def list_open_todos(self, guild_id: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM todo_tasks
                 WHERE guild_id=$1 AND completed=FALSE
                 ORDER BY id
                """,
                guild_id,
            )
            return [dict(r) for r in rows]

    async def claim_todo(self, task_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE todo_tasks
                   SET claimed = array_append(claimed, $2)
                 WHERE id=$1
                   AND completed=FALSE
                   AND NOT (claimed @> ARRAY[$2])
                   AND (max_claims=0 OR array_length(claimed,1) < max_claims)
                """,
                task_id,
                user_id,
            )

    async def unclaim_todo(self, task_id: int, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE todo_tasks
                   SET claimed = array_remove(claimed, $2)
                 WHERE id=$1 AND completed=FALSE
                """,
                task_id,
                user_id,
            )

    async def complete_todo(self, task_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE todo_tasks SET completed=TRUE WHERE id=$1", task_id
            )

    async def remove_todo(self, task_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM todo_tasks WHERE id=$1", task_id)

    async def count_open_claims(self, guild_id: int, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS n
                  FROM todo_tasks
                 WHERE guild_id=$1
                   AND completed=FALSE
                   AND $2 = ANY(claimed)
                """,
                guild_id,
                user_id,
            )
        return row["n"] if row else 0

    async def todo_bonus_map(self, guild_id: int) -> Dict[int, int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT claimed FROM todo_tasks
                 WHERE guild_id=$1
                   AND completed=FALSE
                   AND max_claims>0
                """,
                guild_id,
            )
        bonus: Dict[int, int] = {}
        for r in rows:
            for uid in r["claimed"]:
                bonus[uid] = min(3, bonus.get(uid, 0) + 1)
        return bonus

    # ═══════════════════ FEEDBACK (NEW) ═══════════════════
    # -- anon cooldown --
    async def get_last_anon_ts(self, user_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_ts FROM anon_feedback_cooldown WHERE user_id=$1",
                user_id,
            )
            return row["last_ts"] if row else None

    async def set_last_anon_ts(self, user_id: int, ts):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO anon_feedback_cooldown (user_id, last_ts)
                VALUES ($1,$2)
                ON CONFLICT (user_id) DO UPDATE SET last_ts=$2
                """,
                user_id,
                ts,
            )

    # -- record feedback --
    async def record_feedback(
        self,
        *,
        msg_id: int,
        author_id: int,
        category: str,
        target_id: int | None,
        text: str,
        rating: int | None,
        attachment_urls: list[str] | None,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO feedback
                  (msg_id, author_id, category, target_id,
                   text, rating, attachment_urls)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                RETURNING id
                """,
                msg_id,
                author_id,
                category,
                target_id,
                text,
                rating,
                attachment_urls,
            )
        return row["id"]

    async def update_feedback_status(self, fid: int, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE feedback SET status=$2 WHERE id=$1",
                fid,
                status,
            )

    async def list_feedback_by_author(
        self, author_id: int, limit: int = 25
    ) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, category, status
                  FROM feedback
                 WHERE author_id=$1
                 ORDER BY id DESC
                 LIMIT $2
                """,
                author_id,
                limit,
            )
            return [dict(r) for r in rows]