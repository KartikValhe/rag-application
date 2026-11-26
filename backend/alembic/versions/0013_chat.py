"""
Phase 6 step 6a-8: the chat tables, their row-level security and the chat maintenance role's grants
(docs/phase6-plan.md 9.1, 9.2, 9.7 step 2, C21, C22, C26).

  conversations              one chat of one user about one class; UNIQUE (tenant_id, user_id, id) for the children.
  messages                   questions and answers; the answer row exists from T1 on with outcome 'pending'.
                             UNIQUE (tenant_id, reply_to_id) WHERE role = 'assistant': one answer per question.
  message_citations          server-built snapshots; chunk/document/version ids are NOT foreign keys on purpose.
  message_feedback           one rating per user and answer.
  chat_citation_blank_queue  the document-purge outbox (C21); document_id is NOT a foreign key on purpose.

Row-level security (ENABLE + FORCE) on all five, with three policy sets:
  tenant_isolation  PERMISSIVE, TO app_tenant only (C26: without TO it would also apply to app_chat_maint, and
                    Postgres ORs permissive policies, so the maintenance role's pending condition would be moot).
  chat_owner        RESTRICTIVE, TO app_tenant, user_id = app.user_id (the four content tables; no bypass).
  chat_maint*       PERMISSIVE, TO app_chat_maint, the tenant condition; on messages split into SELECT, DELETE and an
                    UPDATE that matches only assistant rows still 'pending' (C22). Also added to llm_usage_events
                    (created in 0012, which did not need the role).
On messages, chat_tenant_update_pending (RESTRICTIVE, FOR UPDATE, TO app_tenant) limits the API's UPDATE to its own
pending assistant row (6a-8 owner brief).

Grants (least privilege, plan 9.2 as narrowed in 6a-8); nobody gets TRUNCATE, app_platform and app_worker nothing:
  app_tenant      conversations SELECT, INSERT, DELETE, UPDATE (title, last_message_at, message_count)
                  messages SELECT, INSERT, UPDATE (the T3 columns only)
                  message_citations SELECT, INSERT
                  message_feedback SELECT, INSERT, DELETE, UPDATE (rating, comment, updated_at)
                  chat_citation_blank_queue INSERT
  app_chat_maint  SELECT, DELETE on the five tables and llm_usage_events, UPDATE (outcome, partial, completed_at) on
                  messages, UPDATE (snippet, heading_path) on message_citations, USAGE on schema app. Nothing else.

The role app_chat_maint (LOGIN, NOSUPERUSER, NOBYPASSRLS) is created outside migrations (infra/postgres/init.sql,
app/scripts/prepare_test_db.py, the production runbook); this migration only checks that it exists and is plain.

Downgrade drops the maintenance policy and grants on llm_usage_events, the five tables in reverse order (their
grants and policies go with them) and the schema grant: app_chat_maint is left with nothing in this database.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.db.models.chat import (
    CITATION_LOCATION_KINDS,
    CITATION_MAINT_UPDATE_COLUMNS,
    CITATION_SNIPPET_MAX,
    CONVERSATION_TENANT_UPDATE_COLUMNS,
    CONVERSATION_TITLE_MAX,
    FEEDBACK_COMMENT_MAX,
    FEEDBACK_TENANT_UPDATE_COLUMNS,
    MESSAGE_CONTENT_MAX,
    MESSAGE_LANGUAGES,
    MESSAGE_MAINT_UPDATE_COLUMNS,
    MESSAGE_OUTCOMES,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    MESSAGE_ROLES,
    MESSAGE_ROUTES,
    MESSAGE_TENANT_UPDATE_COLUMNS,
    OUTCOME_PENDING,
    ROUTER_CONFIDENCES,
)

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order (parents first); the downgrade drops in reverse.
NEW_TABLES = ("conversations", "messages", "message_citations", "message_feedback", "chat_citation_blank_queue")
OWNER_TABLES = ("conversations", "messages", "message_citations", "message_feedback")
# Tables with one FOR ALL chat_maint policy (messages has three split policies instead).
MAINT_ALL_POLICY_TABLES = ("conversations", "message_citations", "message_feedback", "chat_citation_blank_queue",
                           "llm_usage_events")
MAINT_TABLES = (*NEW_TABLES, "llm_usage_events")

MAINT_ROLE = "app_chat_maint"

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OWNER_PREDICATE = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
PENDING_ASSISTANT = f"role = '{MESSAGE_ROLE_ASSISTANT}' AND outcome = '{OUTCOME_PENDING}'"


def _sql_list(values: Sequence[str]) -> str:
    for v in values:
        if "'" in v:
            raise ValueError("constants used in CHECK constraints must not contain quotes")
    return ", ".join(f"'{v}'" for v in values)


def _columns(values: Sequence[str]) -> str:
    return ", ".join(values)


def _require_role(role: str) -> None:
    conn = op.get_bind()
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
        raise RuntimeError(
            f"Migration 0013 needs the database role '{role}' (created by infra/postgres/init.sql, "
            "app/scripts/prepare_test_db.py or the production runbook), but it does not exist."
        )


def _require_plain_maint_role() -> None:
    """Every policy here is moot for a superuser, a BYPASSRLS role or a member of another role (it would inherit
    that role's grants and the policies written TO it)."""
    conn = op.get_bind()
    row = conn.execute(
        text("""
            SELECT r.rolsuper, r.rolbypassrls,
                   EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)
            FROM pg_roles r WHERE r.rolname = :r
        """),
        {"r": MAINT_ROLE},
    ).one_or_none()
    if row is None or any(row):
        raise RuntimeError(
            f"Migration 0013 needs '{MAINT_ROLE}' to be a plain role: NOSUPERUSER, NOBYPASSRLS and a member of no "
            "other role."
        )


def _secure_table(table: str) -> None:
    """GRANT to app_tenant (narrowed afterwards), ENABLE + FORCE RLS, tenant_isolation TO app_tenant (C26), and no
    anon/authenticated access (the 0008 helper, with the TO clause)."""
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO app_tenant")
    op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON app.{table} AS PERMISSIVE FOR ALL TO app_tenant
            USING      ({TENANT_PREDICATE})
            WITH CHECK ({TENANT_PREDICATE})
    """)
    # On Supabase these API roles exist and must never touch these tables.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON app.{table} FROM anon;
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON app.{table} FROM authenticated;
            END IF;
        END $$
    """)


def _create_tables() -> None:
    op.execute(f"""
        CREATE TABLE app.conversations (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            class_id UUID NOT NULL,
            subject_id UUID,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            message_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_conversations_tenant_user_id UNIQUE (tenant_id, user_id, id),
            CONSTRAINT ck_conversations_title CHECK (title IS NULL OR char_length(title) <= {CONVERSATION_TITLE_MAX}),
            CONSTRAINT ck_conversations_message_count CHECK (message_count >= 0),
            CONSTRAINT fk_conversations_user FOREIGN KEY (tenant_id, user_id)
                REFERENCES app.users(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_conversations_class FOREIGN KEY (tenant_id, class_id)
                REFERENCES app.classes(tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_conversations_subject FOREIGN KEY (tenant_id, subject_id)
                REFERENCES app.subjects(tenant_id, id) ON DELETE SET NULL (subject_id)
        )
    """)
    op.execute("CREATE INDEX ix_conversations_user_last_message ON app.conversations (tenant_id, user_id, last_message_at DESC)")
    op.execute("CREATE INDEX ix_conversations_last_message ON app.conversations (tenant_id, last_message_at)")

    op.execute(f"""
        CREATE TABLE app.messages (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL,
            user_id UUID NOT NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            reply_to_id UUID,
            client_request_id UUID,
            route VARCHAR(16),
            outcome VARCHAR(32),
            partial BOOLEAN NOT NULL DEFAULT false,
            intent VARCHAR(32),
            rule_ids TEXT[],
            router_confidence VARCHAR(8),
            language VARCHAR(8),
            retrieval_query TEXT,
            provider VARCHAR(32),
            model_key VARCHAR(200),
            prompt_version VARCHAR(64),
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            tokens_estimated BOOLEAN NOT NULL DEFAULT false,
            latency_ms JSONB,
            retrieval JSONB,
            invalid_citation_count INTEGER NOT NULL DEFAULT 0,
            flagged_sources_used INTEGER NOT NULL DEFAULT 0,
            needs_review BOOLEAN NOT NULL DEFAULT false,
            guardrail JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_messages_tenant_user_id UNIQUE (tenant_id, user_id, id),
            CONSTRAINT uq_messages_tenant_conversation_id UNIQUE (tenant_id, conversation_id, id),
            CONSTRAINT uq_messages_client_request UNIQUE (tenant_id, conversation_id, client_request_id),
            CONSTRAINT ck_messages_role CHECK (role IN ({_sql_list(MESSAGE_ROLES)})),
            CONSTRAINT ck_messages_content_length CHECK (char_length(content) <= {MESSAGE_CONTENT_MAX}),
            CONSTRAINT ck_messages_route CHECK (route IS NULL OR route IN ({_sql_list(MESSAGE_ROUTES)})),
            CONSTRAINT ck_messages_outcome CHECK (outcome IS NULL OR outcome IN ({_sql_list(MESSAGE_OUTCOMES)})),
            CONSTRAINT ck_messages_language CHECK (language IS NULL OR language IN ({_sql_list(MESSAGE_LANGUAGES)})),
            CONSTRAINT ck_messages_router_confidence
                CHECK (router_confidence IS NULL OR router_confidence IN ({_sql_list(ROUTER_CONFIDENCES)})),
            -- A question replies to nothing and has no outcome; an answer always has both, so the unique reply
            -- index below covers every answer (a NULL reply_to_id would escape it).
            CONSTRAINT ck_messages_shape CHECK (
                (role = '{MESSAGE_ROLE_USER}' AND reply_to_id IS NULL AND outcome IS NULL)
                OR (role = '{MESSAGE_ROLE_ASSISTANT}' AND reply_to_id IS NOT NULL AND outcome IS NOT NULL)
            ),
            CONSTRAINT ck_messages_counts CHECK (
                (prompt_tokens IS NULL OR prompt_tokens >= 0) AND (completion_tokens IS NULL OR completion_tokens >= 0)
                AND invalid_citation_count >= 0 AND flagged_sources_used >= 0
            ),
            CONSTRAINT fk_messages_conversation FOREIGN KEY (tenant_id, user_id, conversation_id)
                REFERENCES app.conversations(tenant_id, user_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_messages_reply_to FOREIGN KEY (tenant_id, conversation_id, reply_to_id)
                REFERENCES app.messages(tenant_id, conversation_id, id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX ux_messages_assistant_reply ON app.messages (tenant_id, reply_to_id) "
        f"WHERE role = '{MESSAGE_ROLE_ASSISTANT}'"
    )
    op.execute("CREATE INDEX ix_messages_conversation_created ON app.messages (tenant_id, conversation_id, created_at)")
    op.execute(
        f"CREATE INDEX ix_messages_pending_created ON app.messages (tenant_id, created_at) WHERE outcome = '{OUTCOME_PENDING}'"
    )

    op.execute(f"""
        CREATE TABLE app.message_citations (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            message_id UUID NOT NULL,
            user_id UUID NOT NULL,
            label VARCHAR(8) NOT NULL,
            ordinal SMALLINT NOT NULL,
            chunk_id UUID NOT NULL,
            document_id UUID NOT NULL,
            version_id UUID NOT NULL,
            document_title VARCHAR(200) NOT NULL,
            version_no INTEGER NOT NULL,
            file_kind VARCHAR(8) NOT NULL,
            location_kind VARCHAR(8) NOT NULL,
            page_start INTEGER,
            page_end INTEGER,
            heading_path TEXT,
            snippet TEXT NOT NULL DEFAULT '',
            flagged BOOLEAN NOT NULL DEFAULT false,
            similarity REAL,
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT ck_message_citations_ordinal CHECK (ordinal >= 1),
            CONSTRAINT ck_message_citations_location_kind CHECK (location_kind IN ({_sql_list(CITATION_LOCATION_KINDS)})),
            CONSTRAINT ck_message_citations_snippet CHECK (char_length(snippet) <= {CITATION_SNIPPET_MAX}),
            CONSTRAINT fk_message_citations_message FOREIGN KEY (tenant_id, user_id, message_id)
                REFERENCES app.messages(tenant_id, user_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_message_citations_document ON app.message_citations (tenant_id, document_id)")

    op.execute(f"""
        CREATE TABLE app.message_feedback (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            message_id UUID NOT NULL,
            user_id UUID NOT NULL,
            rating SMALLINT NOT NULL,
            comment TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id),
            CONSTRAINT uq_message_feedback_message_user UNIQUE (tenant_id, message_id, user_id),
            CONSTRAINT ck_message_feedback_rating CHECK (rating IN (1, -1)),
            CONSTRAINT ck_message_feedback_comment CHECK (comment IS NULL OR char_length(comment) <= {FEEDBACK_COMMENT_MAX}),
            CONSTRAINT fk_message_feedback_message FOREIGN KEY (tenant_id, user_id, message_id)
                REFERENCES app.messages(tenant_id, user_id, id) ON DELETE CASCADE
        )
    """)

    op.execute("""
        CREATE TABLE app.chat_citation_blank_queue (
            tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE CASCADE,
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            document_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, id)
        )
    """)
    op.execute("CREATE INDEX ix_chat_citation_blank_queue_created ON app.chat_citation_blank_queue (tenant_id, created_at)")


def _narrow_tenant_grants() -> None:
    """Plan 9.2 as narrowed in 6a-8: app_tenant keeps only what the request flow and the routes need."""
    op.execute("REVOKE UPDATE ON app.conversations FROM app_tenant")
    op.execute(f"GRANT UPDATE ({_columns(CONVERSATION_TENANT_UPDATE_COLUMNS)}) ON app.conversations TO app_tenant")
    # No DELETE: no route deletes a message; a conversation delete cascades (as the table owner).
    op.execute("REVOKE UPDATE, DELETE ON app.messages FROM app_tenant")
    op.execute(f"GRANT UPDATE ({_columns(MESSAGE_TENANT_UPDATE_COLUMNS)}) ON app.messages TO app_tenant")
    # Citations are never changed by the API after the insert (review 6-0b) and go only by cascade.
    op.execute("REVOKE UPDATE, DELETE ON app.message_citations FROM app_tenant")
    op.execute("REVOKE UPDATE ON app.message_feedback FROM app_tenant")
    op.execute(f"GRANT UPDATE ({_columns(FEEDBACK_TENANT_UPDATE_COLUMNS)}) ON app.message_feedback TO app_tenant")
    # The outbox: INSERT only (C21), so the API can never read what is queued.
    op.execute("REVOKE SELECT, UPDATE, DELETE ON app.chat_citation_blank_queue FROM app_tenant")
    # Nothing for the platform and the worker (they have no grant; this keeps it so if a default privilege exists).
    for table in NEW_TABLES:
        op.execute(f"REVOKE ALL ON app.{table} FROM app_platform, app_worker")


def _tenant_policies() -> None:
    for table in OWNER_TABLES:
        op.execute(f"""
            CREATE POLICY chat_owner ON app.{table} AS RESTRICTIVE FOR ALL TO app_tenant
                USING      ({OWNER_PREDICATE})
                WITH CHECK ({OWNER_PREDICATE})
        """)
    # T3 and the cancel step update the caller's own pending answer, nothing else.
    op.execute(f"""
        CREATE POLICY chat_tenant_update_pending ON app.messages AS RESTRICTIVE FOR UPDATE TO app_tenant
            USING      ({PENDING_ASSISTANT})
            WITH CHECK (role = '{MESSAGE_ROLE_ASSISTANT}')
    """)


def _maint_grants_and_policies() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA app TO {MAINT_ROLE}")
    for table in MAINT_TABLES:
        op.execute(f"GRANT SELECT, DELETE ON app.{table} TO {MAINT_ROLE}")
    op.execute(f"GRANT UPDATE ({_columns(MESSAGE_MAINT_UPDATE_COLUMNS)}) ON app.messages TO {MAINT_ROLE}")
    op.execute(f"GRANT UPDATE ({_columns(CITATION_MAINT_UPDATE_COLUMNS)}) ON app.message_citations TO {MAINT_ROLE}")

    op.execute(f"""
        CREATE POLICY chat_maint_select ON app.messages AS PERMISSIVE FOR SELECT TO {MAINT_ROLE}
            USING ({TENANT_PREDICATE})
    """)
    op.execute(f"""
        CREATE POLICY chat_maint_delete ON app.messages AS PERMISSIVE FOR DELETE TO {MAINT_ROLE}
            USING ({TENANT_PREDICATE})
    """)
    # The sweeper marks stale pending rows: only an assistant row that is still pending can be touched (C22).
    op.execute(f"""
        CREATE POLICY chat_maint_update ON app.messages AS PERMISSIVE FOR UPDATE TO {MAINT_ROLE}
            USING      ({TENANT_PREDICATE} AND {PENDING_ASSISTANT})
            WITH CHECK ({TENANT_PREDICATE})
    """)
    for table in MAINT_ALL_POLICY_TABLES:
        op.execute(f"""
            CREATE POLICY chat_maint ON app.{table} AS PERMISSIVE FOR ALL TO {MAINT_ROLE}
                USING      ({TENANT_PREDICATE})
                WITH CHECK ({TENANT_PREDICATE})
        """)


def upgrade() -> None:
    for role in ("app_tenant", "app_platform", "app_worker", MAINT_ROLE):
        _require_role(role)
    _require_plain_maint_role()

    _create_tables()
    for table in NEW_TABLES:
        _secure_table(table)
    _narrow_tenant_grants()
    _tenant_policies()
    _maint_grants_and_policies()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS chat_maint ON app.llm_usage_events")
    op.execute(f"REVOKE ALL ON app.llm_usage_events FROM {MAINT_ROLE}")
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS app.{table}")
    op.execute(f"REVOKE USAGE ON SCHEMA app FROM {MAINT_ROLE}")
