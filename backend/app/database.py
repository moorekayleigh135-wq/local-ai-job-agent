"""Database engine and session helpers."""

from collections.abc import Generator
import json

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings
from app.security import ENCRYPTED_PREFIX, encrypt_json


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


engine = create_engine(
    settings.mysql_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=settings.app_env == "development",
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and always close it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database_connection() -> bool:
    """Return True when MySQL responds to a trivial query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def migrate_legacy_personal_data(database_engine=engine) -> None:
    """Encrypt profile and search JSON rows written by earlier app versions."""
    json_cast = "CAST(:payload AS JSON)" if database_engine.dialect.name == "mysql" else ":payload"

    def decode_database_json(value: object) -> object:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    with database_engine.begin() as connection:
        profiles = connection.execute(
            text("SELECT id, filename, profile_data FROM candidate_profiles")
        ).mappings().all()
        for row in profiles:
            profile_data = decode_database_json(row["profile_data"])
            if isinstance(profile_data, str) and profile_data.startswith(ENCRYPTED_PREFIX):
                continue
            encrypted = encrypt_json(
                {"filename": row["filename"], "profile": profile_data}
            )
            connection.execute(
                text(
                    "UPDATE candidate_profiles "
                    f"SET filename = 'Encrypted profile', profile_data = {json_cast} "
                    "WHERE id = :id"
                ),
                {"payload": json.dumps(encrypted), "id": row["id"]},
            )

        personal_columns = (
            "role_keywords", "locations", "skills", "work_arrangements", "experience_level"
        )
        selected = ", ".join(("id", *personal_columns))
        preferences = connection.execute(
            text(f"SELECT {selected} FROM search_preferences")
        ).mappings().all()
        for row in preferences:
            updates: dict[str, object] = {"id": row["id"]}
            assignments: list[str] = []
            for column in personal_columns:
                value = decode_database_json(row[column])
                if value is None:
                    continue
                if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX):
                    continue
                updates[column] = json.dumps(
                    encrypt_json(value if column == "experience_level" else value or [])
                )
                assignments.append(f"{column} = {json_cast.replace(':payload', ':' + column)}")
            if assignments:
                connection.execute(
                    text(
                        "UPDATE search_preferences SET "
                        + ", ".join(assignments)
                        + " WHERE id = :id"
                    ),
                    updates,
                )

        matches = connection.execute(
            text("SELECT id, matched_terms FROM job_scan_matches")
        ).mappings().all()
        for row in matches:
            terms = decode_database_json(row["matched_terms"])
            if isinstance(terms, str) and terms.startswith(ENCRYPTED_PREFIX):
                continue
            connection.execute(
                text(
                    "UPDATE job_scan_matches SET matched_terms = "
                    f"{json_cast} WHERE id = :id"
                ),
                {"payload": json.dumps(encrypt_json(terms or [])), "id": row["id"]},
            )


def migrate_notification_posted_at(database_engine=engine) -> None:
    """Add structured source dates to alert rows created by earlier local builds."""
    columns = {column["name"] for column in inspect(database_engine).get_columns("job_notifications")}
    if "posted_at" not in columns:
        with database_engine.begin() as connection:
            connection.execute(text("ALTER TABLE job_notifications ADD COLUMN posted_at DATETIME NULL"))


def migrate_job_external_id(database_engine=engine) -> None:
    """Add source-owned job IDs so query-string postings can be deduplicated."""
    inspector = inspect(database_engine)
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "external_id" not in columns:
        with database_engine.begin() as connection:
            connection.execute(text("ALTER TABLE jobs ADD COLUMN external_id VARCHAR(160) NULL"))
    if not any(index["name"] == "ix_jobs_external_id" for index in inspector.get_indexes("jobs")):
        with database_engine.begin() as connection:
            connection.execute(text("CREATE INDEX ix_jobs_external_id ON jobs (external_id)"))


def migrate_job_salary(database_engine=engine) -> None:
    """Add optional salary and source posting-date fields."""
    columns = {column["name"] for column in inspect(database_engine).get_columns("jobs")}
    definitions = {
        "salary_min": "FLOAT NULL",
        "salary_max": "FLOAT NULL",
        "salary_currency": "VARCHAR(8) NULL",
        "salary_interval": "VARCHAR(24) NULL",
        "posted_at": "DATETIME NULL",
    }
    missing = [(name, definition) for name, definition in definitions.items() if name not in columns]
    if missing:
        with database_engine.begin() as connection:
            for name, definition in missing:
                connection.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {definition}"))


def migrate_search_experience_level(database_engine=engine) -> None:
    """Add the encrypted target experience level to older search profiles."""
    columns = {column["name"] for column in inspect(database_engine).get_columns("search_preferences")}
    if "experience_level" not in columns:
        with database_engine.begin() as connection:
            connection.execute(text("ALTER TABLE search_preferences ADD COLUMN experience_level JSON NULL"))
