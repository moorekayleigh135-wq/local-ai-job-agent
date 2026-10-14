"""Database engine and session helpers."""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


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
