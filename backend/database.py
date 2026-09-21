import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from storage import DATABASE_PATH

IMPORTED_FILE_COLUMNS = """
    file_id,
    original_filename,
    content_type,
    size_bytes,
    sha256,
    storage_path,
    imported_at,
    relative_path,
    technique,
    material_system,
    sample_id,
    measurement_date,
    instrument,
    operator,
    notes,
    substrate,
    measurement_role,
    data_category,
    updated_at,
    archived_at
"""

PROCESSING_RUN_COLUMNS = """
    processing_id,
    file_id,
    model_name,
    model_version,
    parameters_json,
    source_sha256,
    result_path,
    result_sha256,
    processed_at,
    summary_json
"""

REFERENCE_SOURCE_COLUMNS = """
    reference_id,
    original_filename,
    content_type,
    size_bytes,
    sha256,
    storage_path,
    imported_at,
    source_kind,
    technique,
    material_system,
    title,
    citation,
    source_url,
    extraction_status,
    evidence_json
"""

TRAINING_EXAMPLE_COLUMNS = """
    training_id,
    file_id,
    processing_id,
    material_system,
    source_sha256,
    result_sha256,
    confirmed_at,
    confirmed_by,
    active,
    evidence_json
"""

COPILOT_MESSAGE_COLUMNS = """
    message_id,
    file_id,
    role,
    content,
    intent,
    metadata_json,
    created_at
"""

CLARIFICATION_RESPONSE_COLUMNS = """
    clarification_id,
    file_id,
    processing_id,
    question_key,
    question_json,
    response_json,
    status,
    created_at
"""

IMPORT_METADATA_REVISION_COLUMNS = """
    revision_id,
    file_id,
    action,
    previous_json,
    updated_json,
    changed_at
"""

FILE_ATTACHMENT_COLUMNS = """
    attachment_id,
    file_id,
    kind,
    original_filename,
    content_type,
    size_bytes,
    sha256,
    storage_path,
    imported_at
"""

SAMPLE_COLUMNS = """
    sample_id,
    material_system,
    substrate,
    project,
    notes,
    created_at,
    updated_at
"""

ANALYSIS_RECIPE_COLUMNS = """
    recipe_id,
    name,
    config_json,
    created_at,
    updated_at
"""

INSTRUMENT_PRESET_COLUMNS = """
    preset_id,
    name,
    config_json,
    created_at,
    updated_at
"""


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with connect_database() as connection:
        import research_store

        research_store.initialize(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_files (
                file_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                relative_path TEXT,
                technique TEXT,
                material_system TEXT,
                sample_id TEXT,
                measurement_date TEXT,
                instrument TEXT,
                operator TEXT,
                notes TEXT,
                substrate TEXT,
                measurement_role TEXT NOT NULL DEFAULT 'unspecified',
                updated_at TEXT,
                archived_at TEXT
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_runs (
                processing_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                result_path TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id),
                UNIQUE (
                    file_id,
                    source_sha256,
                    model_name,
                    model_version,
                    parameters_json
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processing_runs_file_processed
            ON processing_runs (file_id, processed_at DESC)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reference_sources (
                reference_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                sha256 TEXT NOT NULL UNIQUE,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (
                    source_kind IN ('pdf', 'spectrum')
                ),
                technique TEXT NOT NULL,
                material_system TEXT NOT NULL,
                title TEXT NOT NULL,
                citation TEXT,
                source_url TEXT,
                extraction_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reference_sources_technique
            ON reference_sources (technique, material_system)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_examples (
                training_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                processing_id TEXT NOT NULL,
                material_system TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                evidence_json TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id),
                FOREIGN KEY (processing_id)
                    REFERENCES processing_runs (processing_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_training_examples_active
            ON training_examples (active, material_system, confirmed_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS copilot_messages (
                message_id TEXT PRIMARY KEY,
                file_id TEXT,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                intent TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_copilot_messages_context
            ON copilot_messages (file_id, created_at, message_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS clarification_responses (
                clarification_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                processing_id TEXT NOT NULL,
                question_key TEXT NOT NULL,
                question_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('answered', 'dismissed')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id),
                FOREIGN KEY (processing_id)
                    REFERENCES processing_runs (processing_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_clarification_file_question
            ON clarification_responses (
                file_id, question_key, created_at DESC, clarification_id DESC
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS import_metadata_revisions (
                revision_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (
                    action IN ('edit', 'archive', 'restore', 'substrate_confirmation')
                ),
                previous_json TEXT NOT NULL,
                updated_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_import_revisions_file_changed
            ON import_metadata_revisions (file_id, changed_at, revision_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_attachments (
                attachment_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('optical_image')),
                original_filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES imported_files (file_id),
                UNIQUE (file_id, sha256)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_file_attachments_file_imported
            ON file_attachments (file_id, imported_at, attachment_id)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                sample_id TEXT PRIMARY KEY,
                material_system TEXT,
                substrate TEXT,
                project TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_recipes (
                recipe_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS instrument_presets (
                preset_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_run_tombstones (
                tombstone_id TEXT PRIMARY KEY,
                processing_id TEXT NOT NULL,
                record_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                removed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS substrate_peak_feedback (
                feedback_id TEXT PRIMARY KEY,
                source_file_id TEXT NOT NULL,
                material_system TEXT,
                substrate TEXT NOT NULL CHECK (
                    substrate IN ('glass', 'sio2_si')
                ),
                center_cm_1 REAL NOT NULL,
                half_width_cm_1 REAL NOT NULL CHECK (half_width_cm_1 > 0),
                action TEXT NOT NULL CHECK (action IN ('keep', 'remove')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_file_id) REFERENCES imported_files (file_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_substrate_feedback_scope
            ON substrate_peak_feedback (
                substrate, material_system, center_cm_1, created_at DESC
            )
            """
        )

        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(imported_files)")
        }
        metadata_columns = {
            "relative_path": "TEXT",
            "technique": "TEXT",
            "material_system": "TEXT",
            "sample_id": "TEXT",
            "measurement_date": "TEXT",
            "instrument": "TEXT",
            "operator": "TEXT",
            "notes": "TEXT",
            "substrate": "TEXT",
            "measurement_role": "TEXT NOT NULL DEFAULT 'unspecified'",
            "data_category": "TEXT NOT NULL DEFAULT 'raw_measurement'",
            "updated_at": "TEXT",
            "archived_at": "TEXT",
        }
        for column_name, column_type in metadata_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE imported_files "
                    f"ADD COLUMN {column_name} {column_type}"
                )

        duplicate_checksum = connection.execute(
            """
            SELECT sha256
            FROM imported_files
            GROUP BY sha256
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        if duplicate_checksum is None:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_imported_files_sha256
                ON imported_files (sha256)
                """
            )

        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
                reject_duplicate_imported_file_sha256
            BEFORE INSERT ON imported_files
            WHEN EXISTS (
                SELECT 1
                FROM imported_files
                WHERE sha256 = NEW.sha256
            )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate sha256');
            END
            """
        )

        json_columns = (
            ("processing_runs", "parameters_json"),
            ("processing_runs", "summary_json"),
            ("reference_sources", "evidence_json"),
            ("training_examples", "evidence_json"),
            ("copilot_messages", "metadata_json"),
            ("clarification_responses", "question_json"),
            ("clarification_responses", "response_json"),
            ("import_metadata_revisions", "previous_json"),
            ("import_metadata_revisions", "updated_json"),
            ("analysis_recipes", "config_json"),
            ("instrument_presets", "config_json"),
            ("processing_run_tombstones", "record_json"),
        )
        for table_name, column_name in json_columns:
            for operation in ("INSERT", "UPDATE"):
                trigger_name = (
                    f"validate_{table_name}_{column_name}_{operation.lower()}"
                )
                connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    BEFORE {operation} ON {table_name}
                    WHEN json_valid(NEW.{column_name}) = 0
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            '{table_name}.{column_name} must contain valid JSON'
                        );
                    END
                    """
                )

        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_processing_run_source_mismatch
            BEFORE INSERT ON processing_runs
            WHEN NOT EXISTS (
                SELECT 1
                FROM imported_files
                WHERE file_id = NEW.file_id
                  AND sha256 = NEW.source_sha256
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'processing run source checksum does not match its import'
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS reject_processing_run_update
            BEFORE UPDATE ON processing_runs
            BEGIN
                SELECT RAISE(ABORT, 'processing runs are immutable');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS protect_processed_import_identity
            BEFORE UPDATE OF file_id, size_bytes, sha256, storage_path
            ON imported_files
            WHEN EXISTS (
                SELECT 1
                FROM processing_runs
                WHERE file_id = OLD.file_id
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'processed source identity and checksum are immutable'
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS protect_processed_import_delete
            BEFORE DELETE ON imported_files
            WHEN EXISTS (
                SELECT 1
                FROM processing_runs
                WHERE file_id = OLD.file_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'processed source records cannot be deleted');
            END
            """
        )


def list_imported_files(
    include_archived: bool = False,
    include_analysis: bool = False,
) -> list[dict]:
    with connect_database() as connection:
        analysis_columns = ""
        if include_analysis:
            analysis_columns = """,
                (
                    SELECT processed_at
                    FROM processing_runs
                    WHERE processing_runs.file_id = imported_files.file_id
                    ORDER BY processed_at DESC, processing_id DESC
                    LIMIT 1
                ) AS latest_processed_at,
                (
                    SELECT summary_json
                    FROM processing_runs
                    WHERE processing_runs.file_id = imported_files.file_id
                    ORDER BY processed_at DESC, processing_id DESC
                    LIMIT 1
                ) AS latest_summary_json"""
        records = connection.execute(
            f"""
            SELECT {IMPORTED_FILE_COLUMNS}{analysis_columns}
            FROM imported_files
            {"" if include_archived else "WHERE archived_at IS NULL"}
            ORDER BY imported_at DESC, file_id DESC
            """
        ).fetchall()

    return [dict(record) for record in records]


def get_imported_file(file_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {IMPORTED_FILE_COLUMNS}
            FROM imported_files
            WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()

    return dict(record) if record is not None else None


def find_imported_file_by_sha256(
    connection: sqlite3.Connection,
    sha256: str,
) -> dict | None:
    record = connection.execute(
        f"""
        SELECT {IMPORTED_FILE_COLUMNS}
        FROM imported_files
        WHERE sha256 = ?
        """,
        (sha256,),
    ).fetchone()
    return dict(record) if record is not None else None


def insert_imported_file(
    connection: sqlite3.Connection,
    metadata: dict,
) -> None:
    stored_metadata = {
        "substrate": "unknown",
        "measurement_role": "unspecified",
        "data_category": "raw_measurement",
        "relative_path": None,
        "updated_at": None,
        "archived_at": None,
        **metadata,
    }
    connection.execute(
        """
        INSERT INTO imported_files (
            file_id,
            original_filename,
            content_type,
            size_bytes,
            sha256,
            storage_path,
            imported_at,
            relative_path,
            technique,
            material_system,
            sample_id,
            measurement_date,
            instrument,
            operator,
            notes,
            substrate,
            measurement_role,
            data_category,
            updated_at,
            archived_at
        )
        VALUES (
            :file_id,
            :original_filename,
            :content_type,
            :size_bytes,
            :sha256,
            :storage_path,
            :imported_at,
            :relative_path,
            :technique,
            :material_system,
            :sample_id,
            :measurement_date,
            :instrument,
            :operator,
            :notes,
            :substrate,
            :measurement_role,
            :data_category,
            :updated_at,
            :archived_at
        )
        """,
        stored_metadata,
    )


def update_imported_file_metadata(
    connection: sqlite3.Connection,
    file_id: str,
    updates: dict,
) -> None:
    allowed = {
        "technique",
        "material_system",
        "sample_id",
        "measurement_date",
        "instrument",
        "operator",
        "notes",
        "substrate",
        "measurement_role",
        "data_category",
        "updated_at",
        "archived_at",
    }
    invalid = set(updates) - allowed
    if invalid or not updates:
        raise ValueError("Unsupported or empty imported-file metadata update.")
    assignments = ", ".join(f"{column} = ?" for column in updates)
    connection.execute(
        f"UPDATE imported_files SET {assignments} WHERE file_id = ?",
        (*updates.values(), file_id),
    )


def insert_import_metadata_revision(
    connection: sqlite3.Connection,
    revision: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO import_metadata_revisions (
            revision_id, file_id, action, previous_json, updated_json, changed_at
        ) VALUES (
            :revision_id, :file_id, :action, :previous_json, :updated_json,
            :changed_at
        )
        """,
        revision,
    )


def list_file_attachments(file_id: str) -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {FILE_ATTACHMENT_COLUMNS}
            FROM file_attachments
            WHERE file_id = ?
            ORDER BY imported_at, attachment_id
            """,
            (file_id,),
        ).fetchall()
    return [dict(record) for record in records]


def get_file_attachment(attachment_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {FILE_ATTACHMENT_COLUMNS}
            FROM file_attachments
            WHERE attachment_id = ?
            """,
            (attachment_id,),
        ).fetchone()
    return dict(record) if record is not None else None


def find_file_attachment_by_sha256(
    connection: sqlite3.Connection,
    file_id: str,
    sha256: str,
) -> dict | None:
    record = connection.execute(
        f"""
        SELECT {FILE_ATTACHMENT_COLUMNS}
        FROM file_attachments
        WHERE file_id = ? AND sha256 = ?
        """,
        (file_id, sha256),
    ).fetchone()
    return dict(record) if record is not None else None


def insert_file_attachment(
    connection: sqlite3.Connection,
    attachment: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO file_attachments (
            attachment_id, file_id, kind, original_filename, content_type,
            size_bytes, sha256, storage_path, imported_at
        ) VALUES (
            :attachment_id, :file_id, :kind, :original_filename, :content_type,
            :size_bytes, :sha256, :storage_path, :imported_at
        )
        """,
        attachment,
    )


def list_import_metadata_revisions(file_id: str) -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {IMPORT_METADATA_REVISION_COLUMNS}
            FROM import_metadata_revisions
            WHERE file_id = ?
            ORDER BY changed_at, revision_id
            """,
            (file_id,),
        ).fetchall()
    return [dict(record) for record in records]


def find_processing_run(
    connection: sqlite3.Connection,
    file_id: str,
    source_sha256: str,
    model_name: str,
    model_version: str,
    parameters_json: str,
) -> dict | None:
    record = connection.execute(
        f"""
        SELECT {PROCESSING_RUN_COLUMNS}
        FROM processing_runs
        WHERE file_id = ?
          AND source_sha256 = ?
          AND model_name = ?
          AND model_version = ?
          AND parameters_json = ?
        """,
        (
            file_id,
            source_sha256,
            model_name,
            model_version,
            parameters_json,
        ),
    ).fetchone()
    return dict(record) if record is not None else None


def get_latest_processing_run(file_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {PROCESSING_RUN_COLUMNS}
            FROM processing_runs
            WHERE file_id = ?
            ORDER BY processed_at DESC, processing_id DESC
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
    return dict(record) if record is not None else None


def list_processing_runs(file_id: str) -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {PROCESSING_RUN_COLUMNS}
            FROM processing_runs
            WHERE file_id = ?
            ORDER BY processed_at DESC, processing_id DESC
            """,
            (file_id,),
        ).fetchall()
    return [dict(record) for record in records]


def insert_processing_run(
    connection: sqlite3.Connection,
    processing_run: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO processing_runs (
            processing_id,
            file_id,
            model_name,
            model_version,
            parameters_json,
            source_sha256,
            result_path,
            result_sha256,
            processed_at,
            summary_json
        )
        VALUES (
            :processing_id,
            :file_id,
            :model_name,
            :model_version,
            :parameters_json,
            :source_sha256,
            :result_path,
            :result_sha256,
            :processed_at,
            :summary_json
        )
        """,
        processing_run,
    )


def delete_processing_run(
    connection: sqlite3.Connection,
    processing_id: str,
    reason: str = "incompatible_or_unverifiable_result_replaced",
) -> None:
    record = connection.execute(
        f"SELECT {PROCESSING_RUN_COLUMNS} FROM processing_runs "
        "WHERE processing_id = ?",
        (processing_id,),
    ).fetchone()
    if record is None:
        return
    serialized = json.dumps(
        dict(record),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO processing_run_tombstones (
            tombstone_id, processing_id, record_json, reason, removed_at
        ) VALUES (
            lower(hex(randomblob(16))), ?, ?, ?,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        """,
        (processing_id, serialized, reason),
    )
    connection.execute(
        "DELETE FROM processing_runs WHERE processing_id = ?",
        (processing_id,),
    )


def get_processing_run(processing_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {PROCESSING_RUN_COLUMNS}
            FROM processing_runs
            WHERE processing_id = ?
            """,
            (processing_id,),
        ).fetchone()
    return dict(record) if record is not None else None


def list_processing_run_tombstones() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            """
            SELECT tombstone_id, processing_id, record_json, reason, removed_at
            FROM processing_run_tombstones
            ORDER BY removed_at, tombstone_id
            """
        ).fetchall()
    return [dict(record) for record in records]


def list_substrate_peak_feedback(
    substrate: str | None = None,
    material_system: str | None = None,
) -> list[dict]:
    clauses = []
    values = []
    if substrate is not None:
        clauses.append("substrate = ?")
        values.append(substrate)
    if material_system is not None:
        clauses.append("material_system = ?")
        values.append(material_system)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT feedback_id, source_file_id, material_system, substrate,
                   center_cm_1, half_width_cm_1, action, created_at
            FROM substrate_peak_feedback
            {where}
            ORDER BY created_at, feedback_id
            """,
            values,
        ).fetchall()
    return [dict(record) for record in records]


def insert_substrate_peak_feedback(
    connection: sqlite3.Connection,
    feedback: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO substrate_peak_feedback (
            feedback_id, source_file_id, material_system, substrate,
            center_cm_1, half_width_cm_1, action, created_at
        ) VALUES (
            :feedback_id, :source_file_id, :material_system, :substrate,
            :center_cm_1, :half_width_cm_1, :action, :created_at
        )
        """,
        feedback,
    )


def list_operators() -> list[str]:
    with connect_database() as connection:
        records = connection.execute(
            """
            SELECT operator, MAX(imported_at) AS latest_use
            FROM imported_files
            WHERE operator IS NOT NULL AND trim(operator) <> ''
            GROUP BY operator
            ORDER BY latest_use DESC, operator COLLATE NOCASE
            """
        ).fetchall()
    return [record["operator"] for record in records]


def list_samples(query: str | None = None) -> list[dict]:
    with connect_database() as connection:
        if query:
            records = connection.execute(
                f"""
                SELECT {SAMPLE_COLUMNS}
                FROM samples
                WHERE lower(sample_id) LIKE '%' || lower(?) || '%'
                   OR lower(COALESCE(material_system, ''))
                      LIKE '%' || lower(?) || '%'
                   OR lower(COALESCE(project, '')) LIKE '%' || lower(?) || '%'
                ORDER BY updated_at DESC, sample_id COLLATE NOCASE
                """,
                (query, query, query),
            ).fetchall()
        else:
            records = connection.execute(
                f"""
                SELECT {SAMPLE_COLUMNS}
                FROM samples
                ORDER BY updated_at DESC, sample_id COLLATE NOCASE
                """
            ).fetchall()
    return [dict(record) for record in records]


def upsert_sample(connection: sqlite3.Connection, sample: dict) -> None:
    connection.execute(
        """
        INSERT INTO samples (
            sample_id, material_system, substrate, project, notes,
            created_at, updated_at
        ) VALUES (
            :sample_id, :material_system, :substrate, :project, :notes,
            :created_at, :updated_at
        )
        ON CONFLICT(sample_id) DO UPDATE SET
            material_system = COALESCE(
                NULLIF(excluded.material_system, 'Unknown'),
                samples.material_system
            ),
            substrate = CASE
                WHEN excluded.substrate IS NULL
                  OR excluded.substrate = 'unknown'
                THEN samples.substrate
                ELSE excluded.substrate
            END,
            project = COALESCE(excluded.project, samples.project),
            notes = COALESCE(excluded.notes, samples.notes),
            updated_at = excluded.updated_at
        """,
        sample,
    )


def update_sample_metadata(sample_id: str, updates: dict) -> None:
    allowed = {"material_system", "substrate", "project"}
    invalid = set(updates) - allowed
    if invalid or not updates:
        raise ValueError("Unsupported or empty sample metadata update.")
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with connect_database() as connection:
        result = connection.execute(
            f"""
            UPDATE samples
            SET {assignments}, updated_at = ?
            WHERE sample_id = ?
            """,
            (*updates.values(), datetime.now(timezone.utc).isoformat(), sample_id),
        )
        if result.rowcount == 0:
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO samples (
                    sample_id, material_system, substrate, project, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    sample_id,
                    updates.get("material_system") or "Unknown",
                    updates.get("substrate") or "unknown",
                    updates.get("project"),
                    now,
                    now,
                ),
            )


def _list_config_records(table_name: str, columns: str) -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"SELECT {columns} FROM {table_name} "
            "ORDER BY updated_at DESC, name COLLATE NOCASE"
        ).fetchall()
    return [dict(record) for record in records]


def list_analysis_recipes() -> list[dict]:
    return _list_config_records("analysis_recipes", ANALYSIS_RECIPE_COLUMNS)


def list_instrument_presets() -> list[dict]:
    return _list_config_records("instrument_presets", INSTRUMENT_PRESET_COLUMNS)


def upsert_analysis_recipe(
    connection: sqlite3.Connection,
    recipe: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO analysis_recipes (
            recipe_id, name, config_json, created_at, updated_at
        ) VALUES (
            :recipe_id, :name, :config_json, :created_at, :updated_at
        )
        ON CONFLICT(recipe_id) DO UPDATE SET
            name = excluded.name,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at
        """,
        recipe,
    )


def upsert_instrument_preset(
    connection: sqlite3.Connection,
    preset: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO instrument_presets (
            preset_id, name, config_json, created_at, updated_at
        ) VALUES (
            :preset_id, :name, :config_json, :created_at, :updated_at
        )
        ON CONFLICT(preset_id) DO UPDATE SET
            name = excluded.name,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at
        """,
        preset,
    )


def list_reference_sources(technique: str | None = None) -> list[dict]:
    with connect_database() as connection:
        if technique is None:
            records = connection.execute(
                f"""
                SELECT {REFERENCE_SOURCE_COLUMNS}
                FROM reference_sources
                ORDER BY imported_at DESC, reference_id DESC
                """
            ).fetchall()
        else:
            records = connection.execute(
                f"""
                SELECT {REFERENCE_SOURCE_COLUMNS}
                FROM reference_sources
                WHERE lower(technique) LIKE '%' || lower(?) || '%'
                ORDER BY imported_at DESC, reference_id DESC
                """,
                (technique,),
            ).fetchall()
    return [dict(record) for record in records]


def get_reference_source(reference_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {REFERENCE_SOURCE_COLUMNS}
            FROM reference_sources
            WHERE reference_id = ?
            """,
            (reference_id,),
        ).fetchone()
    return dict(record) if record is not None else None


def find_reference_source_by_sha256(
    connection: sqlite3.Connection,
    sha256: str,
) -> dict | None:
    record = connection.execute(
        f"""
        SELECT {REFERENCE_SOURCE_COLUMNS}
        FROM reference_sources
        WHERE sha256 = ?
        """,
        (sha256,),
    ).fetchone()
    return dict(record) if record is not None else None


def insert_reference_source(
    connection: sqlite3.Connection,
    reference: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO reference_sources (
            reference_id,
            original_filename,
            content_type,
            size_bytes,
            sha256,
            storage_path,
            imported_at,
            source_kind,
            technique,
            material_system,
            title,
            citation,
            source_url,
            extraction_status,
            evidence_json
        ) VALUES (
            :reference_id,
            :original_filename,
            :content_type,
            :size_bytes,
            :sha256,
            :storage_path,
            :imported_at,
            :source_kind,
            :technique,
            :material_system,
            :title,
            :citation,
            :source_url,
            :extraction_status,
            :evidence_json
        )
        """,
        reference,
    )


def list_training_examples(
    active_only: bool = False,
    exclude_file_id: str | None = None,
) -> list[dict]:
    clauses = []
    parameters = []
    if active_only:
        clauses.append("active = 1")
    if exclude_file_id is not None:
        clauses.append("file_id != ?")
        parameters.append(exclude_file_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {TRAINING_EXAMPLE_COLUMNS}
            FROM training_examples
            {where}
            ORDER BY confirmed_at DESC, training_id DESC
            """,
            parameters,
        ).fetchall()
    return [dict(record) for record in records]


def get_active_training_example(file_id: str) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {TRAINING_EXAMPLE_COLUMNS}
            FROM training_examples
            WHERE file_id = ? AND active = 1
            ORDER BY confirmed_at DESC, training_id DESC
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
    return dict(record) if record is not None else None


def insert_training_example(
    connection: sqlite3.Connection,
    example: dict,
) -> None:
    connection.execute(
        "UPDATE training_examples SET active = 0 WHERE file_id = ?",
        (example["file_id"],),
    )
    connection.execute(
        """
        INSERT INTO training_examples (
            training_id, file_id, processing_id, material_system,
            source_sha256, result_sha256, confirmed_at, confirmed_by,
            active, evidence_json
        ) VALUES (
            :training_id, :file_id, :processing_id, :material_system,
            :source_sha256, :result_sha256, :confirmed_at, :confirmed_by,
            :active, :evidence_json
        )
        """,
        example,
    )


def insert_copilot_message(
    connection: sqlite3.Connection,
    message: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO copilot_messages (
            message_id, file_id, role, content, intent,
            metadata_json, created_at
        ) VALUES (
            :message_id, :file_id, :role, :content, :intent,
            :metadata_json, :created_at
        )
        """,
        message,
    )


def get_latest_clarification_response(
    file_id: str,
    question_key: str,
) -> dict | None:
    with connect_database() as connection:
        record = connection.execute(
            f"""
            SELECT {CLARIFICATION_RESPONSE_COLUMNS}
            FROM clarification_responses
            WHERE file_id = ? AND question_key = ?
            ORDER BY created_at DESC, clarification_id DESC
            LIMIT 1
            """,
            (file_id, question_key),
        ).fetchone()
    return dict(record) if record is not None else None


def list_clarification_responses(file_id: str) -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {CLARIFICATION_RESPONSE_COLUMNS}
            FROM clarification_responses
            WHERE file_id = ?
            ORDER BY created_at, clarification_id
            """,
            (file_id,),
        ).fetchall()
    return [dict(record) for record in records]


def list_all_clarification_responses() -> list[dict]:
    with connect_database() as connection:
        records = connection.execute(
            f"""
            SELECT {CLARIFICATION_RESPONSE_COLUMNS}
            FROM clarification_responses
            ORDER BY created_at, clarification_id
            """
        ).fetchall()
    return [dict(record) for record in records]


def insert_clarification_response(
    connection: sqlite3.Connection,
    response: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO clarification_responses (
            clarification_id, file_id, processing_id, question_key,
            question_json, response_json, status, created_at
        ) VALUES (
            :clarification_id, :file_id, :processing_id, :question_key,
            :question_json, :response_json, :status, :created_at
        )
        """,
        response,
    )


def delete_clarification_response(
    connection: sqlite3.Connection,
    clarification_id: str,
) -> None:
    connection.execute(
        "DELETE FROM clarification_responses WHERE clarification_id = ?",
        (clarification_id,),
    )


def list_copilot_messages(file_id: str | None = None) -> list[dict]:
    with connect_database() as connection:
        if file_id is None:
            records = connection.execute(
                f"""
                SELECT {COPILOT_MESSAGE_COLUMNS}
                FROM copilot_messages
                WHERE file_id IS NULL
                ORDER BY created_at, message_id
                """
            ).fetchall()
        else:
            records = connection.execute(
                f"""
                SELECT {COPILOT_MESSAGE_COLUMNS}
                FROM copilot_messages
                WHERE file_id = ?
                ORDER BY created_at, message_id
                """,
                (file_id,),
            ).fetchall()
    return [dict(record) for record in records]


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized at: {DATABASE_PATH}")
