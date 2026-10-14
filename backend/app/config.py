"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "job_agent"
    mysql_user: str = "job_agent"
    mysql_password: str = ""
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:14b"

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
