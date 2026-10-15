from __future__ import annotations

from typing import Literal

from pydantic import Field

from autoheal_common.config import BaseServiceSettings


class ControlSettings(BaseServiceSettings):
    service_name: str = "control"

    # "kubernetes": talk to the API server (in-cluster config, else ~/.kube/config).
    # "none": no cluster (docker compose); cluster views return 503.
    cluster_backend: Literal["kubernetes", "none"] = "kubernetes"
    # Namespace to observe; defaults to the pod's own namespace.
    watch_namespace: str | None = None
    # Only pods carrying this label are considered part of the platform.
    platform_selector: str = "app.kubernetes.io/part-of=autoheal"
    component_label: str = "app.kubernetes.io/component"

    # Failure injection allow-lists (by component label value).
    chaos_http_targets: str = "tasks,gateway"  # components exposing /chaos/*
    chaos_delete_targets: str = "tasks,gateway,redis"  # components whose pods may be deleted
    chaos_target_port: int = 8000
    # Compose mode: "tasks=http://tasks:8000,gateway=http://gateway:8000"
    chaos_direct_targets: str = ""
    chaos_request_timeout_seconds: float = 10.0

    events_limit_default: int = Field(default=50, ge=1, le=500)

    @property
    def namespace(self) -> str:
        return self.watch_namespace or self.pod_namespace

    @staticmethod
    def _csv(raw: str) -> frozenset[str]:
        return frozenset(x.strip() for x in raw.split(",") if x.strip())

    @property
    def http_targets(self) -> frozenset[str]:
        return self._csv(self.chaos_http_targets)

    @property
    def delete_targets(self) -> frozenset[str]:
        return self._csv(self.chaos_delete_targets)

    @property
    def direct_targets(self) -> dict[str, str]:
        pairs = (item.split("=", 1) for item in self._csv(self.chaos_direct_targets) if "=" in item)
        return {k.strip(): v.strip().rstrip("/") for k, v in pairs}
