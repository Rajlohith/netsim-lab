"""Application settings, loaded from environment variables / .env (prefix NETSIM_)."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETSIM_", env_file=".env", extra="ignore")

    app_name: str = "NetSim Lab"
    environment: str = "development"
    api_host: str = "127.0.0.1"  # loopback only by default
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:5173"]

    # Safety defaults for the (future) network layer.
    netns_prefix: str = Field(default="nsl-", min_length=2)
    dry_run: bool = True
    data_dir: Path = Path("data")


@lru_cache
def get_settings() -> Settings:
    return Settings()
