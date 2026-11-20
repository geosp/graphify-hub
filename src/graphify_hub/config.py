"""Configuration, read from the environment (and ``.env``) by pydantic-settings."""

from __future__ import annotations

from pathlib import Path

from mcp_service_kit import BaseServerConfig
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HubSettings(BaseSettings):
    """Hub settings. Every field reads ``HUB_<NAME>``."""

    model_config = SettingsConfigDict(env_prefix="HUB_", extra="ignore")

    data_dir: Path = Field(
        default=Path("/data"), description="Registry, clones and graphs live here."
    )
    token: SecretStr | None = Field(
        default=None, description="Bearer token clients must send over HTTP."
    )
    allowed_repos: str = Field(
        default="https://github.com/",
        description="Comma-separated https:// URL prefixes that may be registered.",
    )
    refresh_interval: int = Field(
        default=900, ge=0, description="Seconds between automatic refreshes; 0 turns them off."
    )
    build_timeout: int = Field(default=900, ge=10, description="Seconds before a build is killed.")
    max_repo_mb: int = Field(default=500, ge=1, description="Largest checkout the hub keeps.")
    max_contexts: int = Field(default=8, ge=1, description="Graphs kept loaded in memory.")
    build_workers: int = Field(default=2, ge=1, le=16, description="Builds that run at once.")

    @field_validator("allowed_repos")
    @classmethod
    def _https_only(cls, value: str) -> str:
        prefixes = [p.strip() for p in value.split(",") if p.strip()]
        if not prefixes:
            raise ValueError("HUB_ALLOWED_REPOS needs at least one prefix")
        for prefix in prefixes:
            if not prefix.startswith("https://") or "@" in prefix:
                raise ValueError(f"allowed repo prefix must be https:// without userinfo: {prefix}")
        return ",".join(prefixes)

    @property
    def allowed_prefixes(self) -> list[str]:
        return self.allowed_repos.split(",")

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"


class HubConfig(BaseModel):
    """Whole-app config: the kit's server section plus the hub's own settings."""

    server: BaseServerConfig
    hub: HubSettings


def load_config() -> HubConfig:
    return HubConfig(server=BaseServerConfig(), hub=HubSettings())
