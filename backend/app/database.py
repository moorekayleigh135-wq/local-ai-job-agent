"""Database engine and session helpers."""

from collections.abc import Generator
import json

from sqlalchemy import create_engine, text
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

        personal_columns = ("role_keywords", "locations", "skills", "work_arrangements")
        selected = ", ".join(("id", *personal_columns))
        preferences = connection.execute(
            text(f"SELECT {selected} FROM search_preferences")
        ).mappings().all()
        for row in preferences:
            updates: dict[str, object] = {"id": row["id"]}
            assignments: list[str] = []
            for column in personal_columns:
                value = decode_database_json(row[column])
                if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX):
                    continue
                updates[column] = json.dumps(encrypt_json(value or []))
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
