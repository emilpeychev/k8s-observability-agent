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


# ──────────────────────────── IaC Resources ───────────────────────────────────


class IaCSource(str, Enum):
    """The IaC tool that produced this resource."""

    TERRAFORM = "terraform"
    HELM = "helm"
    KUSTOMIZE = "kustomize"
    PULUMI = "pulumi"


class IaCResource(BaseModel):
    """An infrastructure or Kubernetes resource extracted from IaC code."""

    source: IaCSource
    source_file: str = ""
    resource_type: str = Field(
        description="IaC resource type, e.g. 'kubernetes_deployment', 'aws_rds_instance', 'helm_release'",
    )
    name: str = ""
    provider: str = Field(
        default="",
        description="Cloud / provider, e.g. 'aws', 'gcp', 'azure', 'kubernetes'",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Key properties extracted from the resource block",
    )

    # Observability relevance
    archetype: str = Field(
        default="",
        description="Inferred workload archetype (database, cache, etc.)",
    )
    monitoring_notes: list[str] = Field(
        default_factory=list,
        description="What monitoring this resource needs",
    )

    @property
    def display_type(self) -> str:
        """Human-readable resource type."""
        return self.resource_type.replace("_", " ").title()


class IaCDiscovery(BaseModel):
    """Aggregated IaC analysis results."""

    resources: list[IaCResource] = Field(default_factory=list)
    helm_releases: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Helm releases found in IaC (chart, repo, values)",
    )
    k8s_resources_from_iac: list[K8sResource] = Field(
        default_factory=list,
        description="K8s resources extracted or rendered from IaC",
    )
    files_scanned: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def has_terraform(self) -> bool:
        return any(r.source == IaCSource.TERRAFORM for r in self.resources)

    @property
    def has_helm(self) -> bool:
        return any(r.source == IaCSource.HELM for r in self.resources) or bool(self.helm_releases)

    @property
    def has_kustomize(self) -> bool:
        return any(r.source == IaCSource.KUSTOMIZE for r in self.resources)

    @property
    def has_pulumi(self) -> bool:
        return any(r.source == IaCSource.PULUMI for r in self.resources)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.resources:
            counts[r.source.value] = counts.get(r.source.value, 0) + 1
        return counts


# ────────────────────────── AWS Discovery ─────────────────────────────────────


class AwsDiscovery(BaseModel):
    """Aggregated AWS resource discovery results."""

    resources: list[IaCResource] = Field(default_factory=list)
    region: str = Field(default="", description="Primary AWS region scanned")
    regions_scanned: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        """Count resources by type (e.g. aws_rds_instance=2, aws_sqs_queue=5)."""
        counts: dict[str, int] = {}
        for r in self.resources:
            counts[r.resource_type] = counts.get(r.resource_type, 0) + 1
        return counts

    @property
    def service_names(self) -> list[str]:
        """Unique AWS service names found."""
        services: set[str] = set()
        for r in self.resources:
            # aws_rds_instance → rds, aws_lambda_function → lambda
            parts = r.resource_type.removeprefix("aws_").split("_")
            services.add(parts[0] if parts else r.resource_type)
        return sorted(services)


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
    iac_discovery: IaCDiscovery | None = Field(
        default=None,
        description="IaC analysis results (Terraform, Helm, Kustomize, Pulumi)",
    )
    aws_discovery: AwsDiscovery | None = Field(
        default=None,
        description="Live AWS resource discovery results",
    )

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
    nodata_state: str = Field(
        default="ok",
        description="Behaviour when the metric is absent: 'ok' (silence), 'alerting' (fire), 'nodata' (mark as nodata)",
    )


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


class GrafanaDashboardRecommendation(BaseModel):
    """A recommended ready-made Grafana community dashboard."""

    dashboard_id: int = Field(description="grafana.com dashboard ID")
    title: str
    description: str = ""
    url: str = Field(default="", description="Direct link to grafana.com/grafana/dashboards/")
    resource: str = Field(default="", description="Qualified name of the K8s resource this relates to")
    archetype: str = Field(default="", description="Workload archetype (database, cache, etc.)")


class ObservabilityPlan(BaseModel):
    """The complete observability plan generated by the agent."""

    platform_summary: str = ""
    metrics: list[MetricRecommendation] = Field(default_factory=list)
    alerts: list[AlertRule] = Field(default_factory=list)
    dashboards: list[DashboardSpec] = Field(default_factory=list)
    dashboard_recommendations: list[GrafanaDashboardRecommendation] = Field(
        default_factory=list,
        description="Ready-made Grafana community dashboards to import",
    )
    recommendations: list[str] = Field(
        default_factory=list, description="Free-form textual recommendations"
    )


# ────────────────────────── Validation Report ─────────────────────────────────


class ValidationCheck(BaseModel):
    """A single validation check performed on the live cluster."""

    name: str
    status: str = Field(description="pass | fail | warn | skip")
    message: str = ""
    fix_applied: bool = False
    fix_description: str = ""
    fix_manifest: str = Field(
        default="",
        description="YAML manifest to apply to fix the issue (kubectl apply -f -)",
    )


class DashboardImportResult(BaseModel):
    """Result of a Grafana dashboard import attempt."""

    dashboard_id: int
    title: str = ""
    url: str = ""
    status: str = Field(default="", description="imported | skipped | failed")


class RemediationStep(BaseModel):
    """A concrete remediation step with optional manifest or command."""

    title: str = Field(description="Short title, e.g. 'Deploy postgres_exporter sidecar'")
    description: str = Field(default="", description="Detailed explanation of what and why")
    command: str = Field(default="", description="Shell command to run, e.g. kubectl apply ...")
    manifest: str = Field(default="", description="Full YAML manifest to apply")
    dashboard_id: int = Field(default=0, description="Grafana.com dashboard ID to import (0=none)")
    dashboard_title: str = Field(default="", description="Dashboard title for display")
    priority: str = Field(default="medium", description="high | medium | low")


class ValidationReport(BaseModel):
    """The report generated by the validation agent after testing a live cluster."""

    cluster_summary: str = ""
    checks: list[ValidationCheck] = Field(default_factory=list)
    dashboards_imported: list[DashboardImportResult] = Field(default_factory=list)
    recommendations: list[str] = Field(
        default_factory=list, description="Remaining action items for the operator"
    )
    remediation_steps: list[RemediationStep] = Field(
        default_factory=list,
        description="Concrete steps with manifests/commands to fix each issue",
    )
    dashboards_to_import: list[DashboardImportResult] = Field(
        default_factory=list,
        description="Recommended Grafana dashboards the operator should import",
    )

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def fixes_applied(self) -> int:
        return sum(1 for c in self.checks if c.fix_applied)
