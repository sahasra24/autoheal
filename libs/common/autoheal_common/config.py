"""Base settings shared by every service. Values come from environment variables."""

from __future__ import annotations

import socket

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    service_name: str = "autoheal"
    environment: str = Field(default="development", description="development | staging | production")
    log_level: str = "INFO"
    log_json: bool = True

    # Identity, injected through the Kubernetes downward API.
    pod_name: str = Field(default_factory=socket.gethostname)
    pod_namespace: str = "default"

    # Failure injection is only for demos. It is off unless explicitly enabled,
    # and it can never be enabled in production (see `chaos_active`).
    chaos_enabled: bool = False
    chaos_token: SecretStr = SecretStr("")

    @property
    def chaos_active(self) -> bool:
        return (
            self.chaos_enabled
            and self.environment.lower() != "production"
            and bool(self.chaos_token.get_secret_value())
        )
