"""Application configuration and settings."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_OUTPUT_DIR = "observability-output"


class Settings(BaseModel):
    """Runtime settings resolved from env vars and CLI flags."""

    # Anthropic
    anthropic_api_key: str = Field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS

    # Repo
    repo_path: str = "."
    github_url: str = ""
    branch: str = "main"

    # Output
    output_dir: str = DEFAULT_OUTPUT_DIR
    output_format: str = "yaml"  # yaml | json

    # Behaviour
    verbose: bool = False
    max_agent_turns: int = 30
    include_patterns: list[str] = Field(
        default_factory=lambda: ["**/*.yaml", "**/*.yml", "**/*.json"],
    )
    exclude_patterns: list[str] = Field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/.git/**",
            "**/vendor/**",
            "**/__pycache__/**",
            "**/charts/**",
            "**/.terraform/**",
            "**/dist/**",
            "**/build/**",
            "**/venv/**",
            "**/.venv/**",
            "**/target/**",
            "**/.github/**",
            "**/.circleci/**",
        ],
    )

    # ── Cluster / validation settings ────────────────────────────────
    kubeconfig: str = Field(
        default="",
        description="Path to kubeconfig file. Empty = use default (~/.kube/config).",
    )
    kube_context: str = Field(
        default="",
        description="Kubernetes context to use. Empty = current context.",
    )
    prometheus_url: str = Field(
        default="",
        description="Prometheus URL (e.g. http://localhost:9090). Empty = auto-discover.",
    )
    grafana_url: str = Field(
        default="",
        description="Grafana URL (e.g. http://localhost:3000). Empty = auto-discover.",
    )
    grafana_api_key: str = Field(
        default_factory=lambda: os.environ.get("GRAFANA_API_KEY", ""),
        description="Grafana API key or service-account token.",
    )
    allow_writes: bool = Field(
        default=False,
        description="Allow the agent to apply manifests to the cluster.",
    )

    @property
    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir).resolve()

    def validate_api_key(self) -> None:
        if not self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Export it as an environment variable or pass --api-key."
            )
