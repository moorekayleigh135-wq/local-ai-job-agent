"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "job_agent"
    mysql_user: str = "job_agent"
    mysql_password: str = ""
    profile_encryption_key: str = ""
    brave_search_api_key: str = ""
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    scan_interval_minutes: int = Field(default=60, ge=15, le=1440)
    company_discovery_interval_hours: int = Field(default=24, ge=6, le=168)
    new_job_window_hours: int = Field(default=48, ge=1, le=168)
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:jobharbor@localhost"

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    @property
    def mysql_url(self) -> str:
        """Build a SQLAlchemy URL, safely escaping special characters in credentials."""
        from urllib.parse import quote_plus

        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://{user}:{password}@{self.mysql_host}:"
            f"{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )


settings = Settings()
