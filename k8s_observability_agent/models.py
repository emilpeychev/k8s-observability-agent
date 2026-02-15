"""Pydantic models for Kubernetes resources and observability recommendations."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────── Kubernetes Resources ────────────────────────────


class K8sResourceKind(str, Enum):
    """Supported Kubernetes resource kinds."""

    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    SERVICE = "Service"
    INGRESS = "Ingress"
    CONFIGMAP = "ConfigMap"
    SECRET = "Secret"
    CRONJOB = "CronJob"
    JOB = "Job"
    NAMESPACE = "Namespace"
    PVC = "PersistentVolumeClaim"
    HPA = "HorizontalPodAutoscaler"
    NETWORK_POLICY = "NetworkPolicy"
    SERVICE_ACCOUNT = "ServiceAccount"
    ROLE = "Role"
    ROLE_BINDING = "RoleBinding"
    CLUSTER_ROLE = "ClusterRole"
    CLUSTER_ROLE_BINDING = "ClusterRoleBinding"
    CUSTOM = "Custom"


class ContainerSpec(BaseModel):
    """A container specification extracted from a workload."""

    name: str
    image: str = ""
    ports: list[int] = Field(default_factory=list)
    env_vars: list[str] = Field(
        default_factory=list, description="Environment variable names (values redacted)"
    )
    resource_requests: dict[str, str] = Field(default_factory=dict)
    resource_limits: dict[str, str] = Field(default_factory=dict)
    liveness_probe: bool = False
    readiness_probe: bool = False
    startup_probe: bool = False

    # Classification (populated by agent.classifier)
    archetype: str = "custom-app"
    archetype_display: str = ""
    archetype_confidence: str = "low"
    archetype_score: float = 0.10
    archetype_match_source: str = "fallback"
    archetype_evidence: list[str] = Field(default_factory=list)


class K8sResource(BaseModel):
    """A single parsed Kubernetes resource."""

    api_version: str = ""
    kind: str
    name: str
    namespace: str = "default"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    source_file: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    # Workload-specific fields (populated for Deployments, StatefulSets, etc.)
    replicas: int | None = None
    containers: list[ContainerSpec] = Field(default_factory=list)
    selector: dict[str, str] = Field(default_factory=dict)

    # Telemetry capabilities detected from the manifest.
    # Populated by the scanner's capability-inference pass.
    # Examples: "exporter:postgres_exporter", "metrics_port:9187",
    #           "scrape_annotations", "builtin_metrics"
    telemetry: list[str] = Field(default_factory=list)

    # Service-specific
    service_type: str | None = None
    service_ports: list[dict[str, Any]] = Field(default_factory=list)

    # Ingress-specific
    ingress_rules: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_workload(self) -> bool:
        return self.kind in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"


# ────────────────────────── Platform Model ────────────────────────────────────


class ServiceRelationship(BaseModel):
    """Describes a relationship between two K8s resources."""

    source: str = Field(description="qualified name of the source resource")
    target: str = Field(description="qualified name of the target resource")
    rel_type: str = Field(description="relationship type, e.g. 'selects', 'exposes', 'mounts'")


class Platform(BaseModel):
    """Aggregated view of a Kubernetes platform discovered from a Git repo."""

    repo_path: str = ""
    resources: list[K8sResource] = Field(default_factory=list)
    relationships: list[ServiceRelationship] = Field(default_factory=list)
    namespaces: list[str] = Field(default_factory=list)
    manifest_files: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def workloads(self) -> list[K8sResource]:
        return [r for r in self.resources if r.is_workload]

    @property
    def services(self) -> list[K8sResource]:
        return [r for r in self.resources if r.kind == "Service"]

    @property
    def has_service_monitors(self) -> bool:
        """True if the repo contains ServiceMonitor or PodMonitor resources."""
        return any(r.kind in ("ServiceMonitor", "PodMonitor") for r in self.resources)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.resources:
            counts[r.kind] = counts.get(r.kind, 0) + 1
        return counts


# ────────────────────────── Observability Output ──────────────────────────────


class MetricRecommendation(BaseModel):
    """A recommended Prometheus metric to collect."""

    metric_name: str
    description: str = ""
    query: str = Field(description="PromQL expression")
    resource: str = Field(description="qualified name of the K8s resource this relates to")


class AlertRule(BaseModel):
    """A recommended Prometheus alerting rule."""

    alert_name: str
    severity: str = "warning"
    expr: str = Field(description="PromQL expression")
    for_duration: str = "5m"
    summary: str = ""
    description: str = ""
    resource: str = ""


class DashboardPanel(BaseModel):
    """A recommended Grafana dashboard panel."""

    title: str
    panel_type: str = "graph"
    queries: list[str] = Field(default_factory=list, description="PromQL queries")
    description: str = ""
    resource: str = ""


class DashboardSpec(BaseModel):
    """A full Grafana dashboard specification."""

    title: str
    description: str = ""
    panels: list[DashboardPanel] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ObservabilityPlan(BaseModel):
    """The complete observability plan generated by the agent."""

    platform_summary: str = ""
    metrics: list[MetricRecommendation] = Field(default_factory=list)
    alerts: list[AlertRule] = Field(default_factory=list)
    dashboards: list[DashboardSpec] = Field(default_factory=list)
    recommendations: list[str] = Field(
        default_factory=list, description="Free-form textual recommendations"
    )
