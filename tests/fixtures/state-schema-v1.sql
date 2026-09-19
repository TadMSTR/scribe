-- Schema 1, as scribe v0.2.0 actually created it.
--
-- GENERATED, NOT HAND-WRITTEN. The body below is the `_SCHEMA` literal from
-- `src/scribe/state.py` at tag v0.2.0 (0e78420c3c23), the last release whose
-- SCHEMA_VERSION was 1, plus the `user_version` stamp that version's `Store.__init__`
-- applied. Extracted with `ast.literal_eval`, so it is the string the interpreter saw
-- rather than a transcription of it.
--
-- WHY A COMMITTED FILE RATHER THAN `git show` AT TEST TIME. CI checks out with
-- fetch-depth 0 so the tag IS reachable there, and tests/test_schema_migration.py
-- re-derives this file and asserts it still matches. But a test that can only run with
-- full history is a test that silently stops running the moment a checkout changes, so
-- the fixture is the primary artefact and the git check is the guard on it.
--
-- Regenerate: python tests/fixtures/regenerate_schema_v1.py

CREATE TABLE IF NOT EXISTS sessions (
    transcript_path TEXT PRIMARY KEY,
    -- Written when a session is summarized, not when it is observed: `scan` upserts a row
    -- from a filesystem stat, and the session id is inside the transcript. It was empty on
    -- all 441 rows until vikunja#872, because nothing ever supplied it after extraction --
    -- an always-empty column that reads like a usable key. `transcript_path` is the primary
    -- key and the join key; this is for correlating a row with a digest anchor.
    session_id      TEXT,
    agent           TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime_ns        INTEGER NOT NULL DEFAULT 0,
    last_offset     INTEGER NOT NULL DEFAULT 0,
    last_turn_uuid  TEXT,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status);

-- One row per turn that has made it into a written digest. The PRIMARY KEY is the
-- idempotency guarantee: writing the same turn twice is a no-op, not a duplicate block.
CREATE TABLE IF NOT EXISTS processed_turns (
    turn_uuid       TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    written_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS processed_turns_path ON processed_turns(transcript_path);

PRAGMA user_version = 1;
