"""Shared building blocks for AutoHeal services.

Every service gets the same observability contract from this package:
structured JSON logs carrying a correlation ID, RED metrics for every route,
Kubernetes-style health endpoints, and (in development) a guarded
failure-injection API used to demonstrate self-healing.
"""

__version__ = "1.0.0"
