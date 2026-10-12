"""add chat_search_content extraction column and trigram index for fast PostgreSQL chat search

Revision ID: 110c9917a168
Revises: b8f2c6d91a04
Create Date: 2026-10-12 10:00:00.000000

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '110c9917a168'
down_revision: str | None = 'b8f2c6d91a04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'chat_search_content_trgm_idx'


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != 'postgresql':
        return

    log = logging.getLogger(__name__)

    # pg_trgm backs the trigram index below. On managed PostgreSQL creating an
    # extension may require elevated privileges; without it the search code
    # keeps using the legacy path, so a failure here is not fatal.
    try:
        with conn.begin_nested():
            op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    except sa.exc.DBAPIError as exc:
        if exc.connection_invalidated:
            raise
        log.warning('Could not create pg_trgm extension: %s', exc)

    # Extracts the plain text of every message in a chat JSON blob. Searching
    # this small column avoids detoasting the (potentially multi-MB) chat.chat
    # blobs. Malformed rows yield '' instead of raising so a single bad chat
    # can never break writes.
    op.execute("""
        CREATE OR REPLACE FUNCTION chat_search_extract(j json)
        RETURNS text
        LANGUAGE plpgsql
        IMMUTABLE
        AS $function$
        DECLARE
          result text;
        BEGIN
          SELECT string_agg(DISTINCT content, ' ') INTO result FROM (
            SELECT e.value->>'content' AS content
            FROM json_each(j #> '{history,messages}') AS e
            WHERE json_typeof(e.value->'content') = 'string'
            UNION ALL
            SELECT m.value->>'content'
            FROM json_array_elements(j->'messages') AS m
            WHERE json_typeof(m.value->'content') = 'string'
          ) contents
          WHERE contents.content IS NOT NULL;
          RETURN coalesce(result, '');
        EXCEPTION WHEN OTHERS THEN
          RETURN '';
        END;
        $function$
        """)

    op.add_column('chat', sa.Column('chat_search_content', sa.Text(), nullable=True), if_not_exists=True)

    op.execute("""
        CREATE OR REPLACE FUNCTION chat_search_content_maintain()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          NEW.chat_search_content := chat_search_extract(NEW.chat);
          RETURN NEW;
        END;
        $function$
        """)
    op.execute('DROP TRIGGER IF EXISTS chat_search_content_trigger ON chat')
    op.execute("""
        CREATE TRIGGER chat_search_content_trigger
        BEFORE INSERT OR UPDATE OF chat ON chat
        FOR EACH ROW EXECUTE FUNCTION chat_search_content_maintain()
        """)

    # Backfill existing rows; the trigger maintains the column from here on.
    op.execute('UPDATE chat SET chat_search_content = chat_search_extract(chat) WHERE chat_search_content IS NULL')

    try:
        with conn.begin_nested():
            op.execute(
                f'CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON chat USING gin (lower(chat_search_content) gin_trgm_ops)'
            )
    except sa.exc.DBAPIError as exc:
        if exc.connection_invalidated:
            raise
        # Alembic records a skipped attempt too; it will not retry on every startup.
        log.warning('Skipped PostgreSQL index %s: %s', INDEX_NAME, exc)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != 'postgresql':
        return

    op.execute('DROP TRIGGER IF EXISTS chat_search_content_trigger ON chat')
    op.execute('DROP FUNCTION IF EXISTS chat_search_content_maintain()')
    op.execute(f'DROP INDEX IF EXISTS {INDEX_NAME}')
    op.drop_column('chat', 'chat_search_content', if_exists=True)
    op.execute('DROP FUNCTION IF EXISTS chat_search_extract(json)')
    # The pg_trgm extension is intentionally kept: it may predate this
    # migration or be used by other objects.
