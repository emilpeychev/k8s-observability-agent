"""Infrastructure-as-Code analysers for Terraform, Helm, Kustomize, and Pulumi.

Each analyser:
1. Discovers relevant files in the repo
2. Parses them and extracts infrastructure / K8s resources
3. Infers observability archetypes so the agent knows what monitoring is needed

All parsing is **static** — no external tools are required.  When ``helm`` or
``kubectl`` binaries are available, the Helm / Kustomize parsers will call them
to render full manifests and feed the results into the normal K8s scanner.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from k8s_observability_agent.models import (
    IaCDiscovery,
    IaCResource,
    IaCSource,
    K8sResource,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  INFRASTRUCTURE → ARCHETYPE MAPPING
# ══════════════════════════════════════════════════════════════════════════════

# Maps IaC resource types to (archetype, monitoring_notes) tuples.
_INFRA_ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    # ── AWS ───────────────────────────────────────────────────────────────
    "aws_db_instance": ("database", ["Needs CloudWatch or postgres_exporter/mysqld_exporter", "Monitor replication lag, connections, IOPS"]),
    "aws_rds_cluster": ("database", ["Needs CloudWatch or postgres_exporter/mysqld_exporter", "Monitor replication lag, connections, IOPS"]),
    "aws_rds_cluster_instance": ("database", ["Instance-level monitoring", "Monitor CPU, memory, disk"]),
    "aws_elasticache_cluster": ("cache", ["Needs CloudWatch or redis_exporter/memcached_exporter", "Monitor hit rate, evictions, memory"]),
    "aws_elasticache_replication_group": ("cache", ["Needs redis_exporter", "Monitor replication, failover, memory"]),
    "aws_mq_broker": ("message-queue", ["Needs CloudWatch or rabbitmq_exporter", "Monitor queue depth, consumers, message rates"]),
    "aws_msk_cluster": ("message-queue", ["Needs kafka_exporter or JMX", "Monitor consumer lag, partition count, ISR"]),
    "aws_elasticsearch_domain": ("search-engine", ["Needs CloudWatch or elasticsearch_exporter", "Monitor cluster health, indexing rate, search latency"]),
    "aws_opensearch_domain": ("search-engine", ["Needs CloudWatch or elasticsearch_exporter", "Monitor cluster health, indexing rate, search latency"]),
    "aws_sqs_queue": ("message-queue", ["Needs CloudWatch", "Monitor queue depth, age of oldest message"]),
    "aws_sns_topic": ("message-queue", ["Needs CloudWatch", "Monitor delivery failures, message count"]),
    "aws_ecs_service": ("custom-app", ["Needs CloudWatch Container Insights", "Monitor task count, CPU, memory"]),
    "aws_lambda_function": ("custom-app", ["Needs CloudWatch", "Monitor invocations, errors, duration, throttles"]),
    "aws_s3_bucket": ("custom-app", ["Optional CloudWatch", "Monitor request count, errors, latency if heavily used"]),

    # ── GCP ───────────────────────────────────────────────────────────────
    "google_sql_database_instance": ("database", ["Needs Cloud Monitoring or postgres_exporter/mysqld_exporter", "Monitor connections, replication lag, disk"]),
    "google_redis_instance": ("cache", ["Needs Cloud Monitoring or redis_exporter", "Monitor hit rate, evictions, memory"]),
    "google_pubsub_topic": ("message-queue", ["Needs Cloud Monitoring", "Monitor message backlog, delivery latency"]),
    "google_pubsub_subscription": ("message-queue", ["Needs Cloud Monitoring", "Monitor unacked messages, delivery latency"]),
    "google_container_cluster": ("custom-app", ["GKE cluster — needs kube-state-metrics, node-exporter", "Monitor node health, pod scheduling, API server"]),

    # ── Azure ─────────────────────────────────────────────────────────────
    "azurerm_postgresql_server": ("database", ["Needs Azure Monitor or postgres_exporter", "Monitor connections, replication, storage"]),
    "azurerm_postgresql_flexible_server": ("database", ["Needs Azure Monitor or postgres_exporter", "Monitor connections, replication, storage"]),
    "azurerm_mysql_server": ("database", ["Needs Azure Monitor or mysqld_exporter", "Monitor connections, replication, storage"]),
    "azurerm_mysql_flexible_server": ("database", ["Needs Azure Monitor or mysqld_exporter", "Monitor connections, replication, storage"]),
    "azurerm_redis_cache": ("cache", ["Needs Azure Monitor or redis_exporter", "Monitor hit rate, evictions, memory, connections"]),
    "azurerm_cosmosdb_account": ("database", ["Needs Azure Monitor", "Monitor RU consumption, latency, availability"]),
    "azurerm_servicebus_namespace": ("message-queue", ["Needs Azure Monitor", "Monitor queue depth, message count, dead letters"]),
    "azurerm_eventhub_namespace": ("message-queue", ["Needs Azure Monitor", "Monitor throughput units, incoming/outgoing messages"]),
    "azurerm_kubernetes_cluster": ("custom-app", ["AKS cluster — needs kube-state-metrics, node-exporter", "Monitor node health, pod scheduling"]),

    # ── Kubernetes provider ───────────────────────────────────────────────
    "kubernetes_deployment": ("custom-app", ["Standard K8s workload", "Monitor replicas, restarts, CPU/memory"]),
    "kubernetes_deployment_v1": ("custom-app", ["Standard K8s workload", "Monitor replicas, restarts, CPU/memory"]),
    "kubernetes_stateful_set": ("custom-app", ["Stateful workload", "Monitor volume usage, pod identity, restarts"]),
    "kubernetes_stateful_set_v1": ("custom-app", ["Stateful workload", "Monitor volume usage, pod identity, restarts"]),
    "kubernetes_daemon_set": ("custom-app", ["DaemonSet", "Monitor desired vs current, node coverage"]),
    "kubernetes_daemon_set_v1": ("custom-app", ["DaemonSet", "Monitor desired vs current, node coverage"]),
    "kubernetes_service": ("custom-app", ["K8s Service", "Monitor endpoints, latency if behind mesh"]),
    "kubernetes_service_v1": ("custom-app", ["K8s Service", "Monitor endpoints, latency if behind mesh"]),
    "kubernetes_ingress": ("reverse-proxy", ["Ingress controller", "Monitor request rate, error rate, latency"]),
    "kubernetes_ingress_v1": ("reverse-proxy", ["Ingress controller", "Monitor request rate, error rate, latency"]),
    "kubernetes_namespace": ("custom-app", []),
    "kubernetes_namespace_v1": ("custom-app", []),
    "kubernetes_config_map": ("custom-app", []),
    "kubernetes_config_map_v1": ("custom-app", []),
    "kubernetes_secret": ("custom-app", []),
    "kubernetes_secret_v1": ("custom-app", []),
}

# Helm chart name → archetype
_HELM_CHART_ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    "postgresql": ("database", ["Deploy postgres_exporter sidecar", "Import dashboard 9628"]),
    "mysql": ("database", ["Deploy mysqld_exporter sidecar", "Import dashboard 7362"]),
    "mariadb": ("database", ["Deploy mysqld_exporter sidecar", "Import dashboard 7362"]),
    "mongodb": ("database", ["Deploy mongodb_exporter sidecar", "Import dashboard 2583"]),
    "redis": ("cache", ["Deploy redis_exporter sidecar", "Import dashboard 11835"]),
    "memcached": ("cache", ["Deploy memcached_exporter sidecar"]),
    "rabbitmq": ("message-queue", ["Built-in Prometheus metrics", "Import dashboard 10991"]),
    "kafka": ("message-queue", ["Deploy kafka_exporter", "Import dashboard 7589"]),
    "nats": ("message-queue", ["Built-in /metrics endpoint", "Import dashboard 2279"]),
    "elasticsearch": ("search-engine", ["Deploy elasticsearch_exporter", "Import dashboard 4358"]),
    "opensearch": ("search-engine", ["Deploy elasticsearch_exporter"]),
    "nginx": ("web-server", ["Import dashboard 9614"]),
    "nginx-ingress": ("reverse-proxy", ["Import NGINX Ingress dashboard 9614"]),
    "ingress-nginx": ("reverse-proxy", ["Import NGINX Ingress dashboard 9614"]),
    "traefik": ("reverse-proxy", ["Built-in /metrics endpoint"]),
    "prometheus": ("monitoring", ["Self-monitoring"]),
    "grafana": ("monitoring", ["Grafana self-monitoring"]),
    "loki": ("logging", ["Loki metrics"]),
    "argocd": ("custom-app", ["Built-in metrics", "Import dashboard 14584"]),
    "cert-manager": ("custom-app", ["Built-in metrics", "Import dashboard 11001"]),
    "harbor": ("custom-app", ["Deploy postgres_exporter, redis_exporter", "Import dashboard 14075"]),
    "minio": ("custom-app", ["Built-in /minio/v2/metrics", "Import dashboard 13502"]),
    "istio": ("reverse-proxy", ["Built-in Envoy metrics", "Import dashboard 7639"]),
    "consul": ("custom-app", ["Built-in /metrics endpoint"]),
    "vault": ("custom-app", ["Built-in /v1/sys/metrics"]),
    "tekton-pipelines": ("custom-app", ["Built-in metrics", "Import dashboard 15698"]),
}

# Pulumi resource type → archetype
_PULUMI_ARCHETYPES: dict[str, tuple[str, list[str]]] = {
    "aws:rds:Instance": ("database", ["Needs postgres_exporter/mysqld_exporter"]),
    "aws:rds:Cluster": ("database", ["Needs postgres_exporter/mysqld_exporter"]),
    "aws:elasticache:Cluster": ("cache", ["Needs redis_exporter"]),
    "aws:elasticache:ReplicationGroup": ("cache", ["Needs redis_exporter"]),
    "aws:mq:Broker": ("message-queue", ["Needs rabbitmq_exporter"]),
    "aws:msk:Cluster": ("message-queue", ["Needs kafka_exporter"]),
    "aws:elasticsearch:Domain": ("search-engine", ["Needs elasticsearch_exporter"]),
    "aws:sqs:Queue": ("message-queue", ["Monitor via CloudWatch"]),
    "gcp:sql:DatabaseInstance": ("database", ["Needs postgres_exporter/mysqld_exporter"]),
    "gcp:redis:Instance": ("cache", ["Needs redis_exporter"]),
    "azure:postgresql:Server": ("database", ["Needs postgres_exporter"]),
    "azure:redis:Cache": ("cache", ["Needs redis_exporter"]),
    "kubernetes:apps/v1:Deployment": ("custom-app", ["Standard K8s workload"]),
    "kubernetes:apps/v1:StatefulSet": ("custom-app", ["Stateful workload"]),
    "kubernetes:apps/v1:DaemonSet": ("custom-app", ["DaemonSet"]),
}


# ══════════════════════════════════════════════════════════════════════════════
#  TERRAFORM PARSER
# ══════════════════════════════════════════════════════════════════════════════


def _parse_terraform_file(path: Path, repo_root: Path) -> list[IaCResource]:
    """Parse a single .tf file using python-hcl2 (if available) or regex fallback."""
    rel = str(path.relative_to(repo_root))
    resources: list[IaCResource] = []

    try:
        import hcl2  # type: ignore[import-untyped]

        with open(path) as f:
            parsed = hcl2.load(f)

        for resource_block in parsed.get("resource", []):
            for res_type, instances in resource_block.items():
                for instance in instances if isinstance(instances, list) else [instances]:
                    for res_name, body in instance.items():
                        if isinstance(body, list):
                            body = body[0] if body else {}
                        archetype, notes = _INFRA_ARCHETYPES.get(
                            res_type, ("custom-app", [])
                        )
                        # Extract key properties
                        props: dict[str, Any] = {}
                        for key in ("engine", "engine_version", "instance_class",
                                    "node_type", "image", "chart", "repository",
                                    "namespace", "replicas", "allocated_storage",
                                    "cluster_identifier", "name"):
                            if key in body:
                                val = body[key]
                                # HCL2 wraps values in lists
                                if isinstance(val, list) and len(val) == 1:
                                    val = val[0]
                                props[key] = val

                        resources.append(IaCResource(
                            source=IaCSource.TERRAFORM,
                            source_file=rel,
                            resource_type=res_type,
                            name=res_name,
                            provider=res_type.split("_")[0] if "_" in res_type else "unknown",
                            properties=props,
                            archetype=archetype,
                            monitoring_notes=list(notes),
                        ))

    except ImportError:
        # Fallback: regex-based extraction without hcl2
        resources = _parse_terraform_regex(path, repo_root)
    except Exception as exc:
        logger.warning("Failed to parse Terraform file %s: %s", rel, exc)
        resources = _parse_terraform_regex(path, repo_root)

    return resources


def _parse_terraform_regex(path: Path, repo_root: Path) -> list[IaCResource]:
    """Regex fallback for Terraform parsing when python-hcl2 is not installed."""
    rel = str(path.relative_to(repo_root))
    resources: list[IaCResource] = []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return resources

    # Match: resource "type" "name" {
    pattern = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
    for match in pattern.finditer(text):
        res_type = match.group(1)
        res_name = match.group(2)
        archetype, notes = _INFRA_ARCHETYPES.get(res_type, ("custom-app", []))

        # Try to extract simple string properties from the block
        block_start = match.end()
        props = _extract_tf_block_props(text, block_start)

        resources.append(IaCResource(
            source=IaCSource.TERRAFORM,
            source_file=rel,
            resource_type=res_type,
            name=res_name,
            provider=res_type.split("_")[0] if "_" in res_type else "unknown",
            properties=props,
            archetype=archetype,
            monitoring_notes=list(notes),
        ))

    return resources


def _extract_tf_block_props(text: str, start: int) -> dict[str, Any]:
    """Extract top-level key = value pairs from a Terraform block (best effort)."""
    props: dict[str, Any] = {}
    depth = 1
    i = start
    wanted_keys = {
        "engine", "engine_version", "instance_class", "node_type",
        "image", "chart", "repository", "namespace", "replicas",
        "allocated_storage", "name", "cluster_identifier",
    }

    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
        if depth > 1:
            continue

    block_text = text[start:i]
    for key in wanted_keys:
        # Match: key = "value" or key = value
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', block_text, re.MULTILINE)
        if m:
            props[key] = m.group(1)
        else:
            m = re.search(rf'^\s*{key}\s*=\s*(\S+)', block_text, re.MULTILINE)
            if m:
                val = m.group(1).strip('"')
                if val not in ("{", "["):
                    props[key] = val

    return props


def _discover_terraform(repo_root: Path) -> list[IaCResource]:
    """Find and parse all .tf files in the repo."""
    resources: list[IaCResource] = []
    for tf_file in repo_root.rglob("*.tf"):
        # Skip common non-user directories
        rel = str(tf_file.relative_to(repo_root))
        if any(part.startswith(".") for part in tf_file.parts):
            continue
        if "vendor" in tf_file.parts or "node_modules" in tf_file.parts:
            continue
        resources.extend(_parse_terraform_file(tf_file, repo_root))
    return resources


# ══════════════════════════════════════════════════════════════════════════════
#  HELM PARSER
# ══════════════════════════════════════════════════════════════════════════════


def _discover_helm_charts(repo_root: Path) -> tuple[list[IaCResource], list[dict[str, Any]], list[K8sResource]]:
    """Discover Helm charts and extract observability-relevant information.

    Returns (iac_resources, helm_releases, k8s_resources_from_templates).
    """
    iac_resources: list[IaCResource] = []
    helm_releases: list[dict[str, Any]] = []
    k8s_resources: list[K8sResource] = []

    for chart_yaml in repo_root.rglob("Chart.yaml"):
        rel_dir = str(chart_yaml.parent.relative_to(repo_root))
        if any(part.startswith(".") for part in chart_yaml.parts):
            continue
        try:
            chart_data = yaml.safe_load(chart_yaml.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", chart_yaml, exc)
            continue

        if not isinstance(chart_data, dict):
            continue

        chart_name = chart_data.get("name", "unknown")
        chart_version = chart_data.get("version", "")

        # Check chart archetype
        archetype, notes = "", []
        for pattern, (arch, n) in _HELM_CHART_ARCHETYPES.items():
            if pattern in chart_name.lower():
                archetype, notes = arch, n
                break

        iac_resources.append(IaCResource(
            source=IaCSource.HELM,
            source_file=str(chart_yaml.relative_to(repo_root)),
            resource_type="helm_chart",
            name=chart_name,
            provider="helm",
            properties={
                "version": chart_version,
                "description": chart_data.get("description", ""),
                "app_version": chart_data.get("appVersion", ""),
                "type": chart_data.get("type", "application"),
            },
            archetype=archetype,
            monitoring_notes=list(notes),
        ))

        # Parse dependencies
        for dep in chart_data.get("dependencies", []):
            dep_name = dep.get("name", "")
            dep_arch, dep_notes = "", []
            for pattern, (arch, n) in _HELM_CHART_ARCHETYPES.items():
                if pattern in dep_name.lower():
                    dep_arch, dep_notes = arch, n
                    break
            if dep_name:
                helm_releases.append({
                    "chart": dep_name,
                    "version": dep.get("version", ""),
                    "repository": dep.get("repository", ""),
                    "parent_chart": chart_name,
                })
                if dep_arch:
                    iac_resources.append(IaCResource(
                        source=IaCSource.HELM,
                        source_file=str(chart_yaml.relative_to(repo_root)),
                        resource_type="helm_dependency",
                        name=dep_name,
                        provider="helm",
                        properties=dep,
                        archetype=dep_arch,
                        monitoring_notes=list(dep_notes),
                    ))

        # Parse values.yaml for image references
        values_yaml = chart_yaml.parent / "values.yaml"
        if values_yaml.exists():
            _extract_helm_values(values_yaml, repo_root, chart_name, iac_resources)

        # Try rendering templates with `helm template`
        rendered = _render_helm_chart(chart_yaml.parent, chart_name)
        k8s_resources.extend(rendered)

    return iac_resources, helm_releases, k8s_resources


def _extract_helm_values(
    values_path: Path,
    repo_root: Path,
    chart_name: str,
    resources: list[IaCResource],
) -> None:
    """Extract image references and config from values.yaml."""
    try:
        data = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    except Exception:
        return

    if not isinstance(data, dict):
        return

    rel = str(values_path.relative_to(repo_root))
    images = _find_images_in_dict(data)
    for img in images:
        resources.append(IaCResource(
            source=IaCSource.HELM,
            source_file=rel,
            resource_type="helm_image_ref",
            name=f"{chart_name}/{img}",
            provider="helm",
            properties={"image": img, "chart": chart_name},
        ))


def _find_images_in_dict(d: dict, prefix: str = "") -> list[str]:
    """Recursively find image references in a nested dict (Helm values style)."""
    images: list[str] = []
    if not isinstance(d, dict):
        return images

    # Common patterns: image.repository + image.tag, or image: "..."
    if "repository" in d and "tag" in d:
        repo = d["repository"]
        tag = d["tag"]
        if isinstance(repo, str) and repo:
            images.append(f"{repo}:{tag}" if tag else repo)
    elif "image" in d:
        img = d["image"]
        if isinstance(img, str) and img and "/" in img:
            images.append(img)
        elif isinstance(img, dict):
            images.extend(_find_images_in_dict(img))

    for key, val in d.items():
        if key in ("repository", "tag", "image"):
            continue
        if isinstance(val, dict):
            images.extend(_find_images_in_dict(val, prefix=f"{prefix}{key}."))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    images.extend(_find_images_in_dict(item, prefix=f"{prefix}{key}[]."))

    return images


def _render_helm_chart(chart_dir: Path, chart_name: str) -> list[K8sResource]:
    """Try `helm template` to render full K8s manifests from a chart."""
    if not shutil.which("helm"):
        logger.debug("helm binary not found — skipping template rendering for %s", chart_name)
        return []

    try:
        result = subprocess.run(
            ["helm", "template", chart_name, str(chart_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("helm template failed for %s: %s", chart_name, result.stderr[:200])
            return []

        # Parse the rendered YAML — import here to avoid circular imports
        from k8s_observability_agent.scanner import parse_manifest_file as _parse

        resources: list[K8sResource] = []
        for doc in yaml.safe_load_all(result.stdout):
            if doc is None:
                continue
            if isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc and "metadata" in doc:
                from k8s_observability_agent.scanner import _parse_resource
                res = _parse_resource(doc, f"helm:{chart_name}")
                resources.append(res)
        return resources

    except Exception as exc:
        logger.debug("helm template error for %s: %s", chart_name, exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  KUSTOMIZE PARSER
# ══════════════════════════════════════════════════════════════════════════════


def _discover_kustomize(repo_root: Path) -> tuple[list[IaCResource], list[K8sResource]]:
    """Discover kustomization.yaml files and parse or render them."""
    iac_resources: list[IaCResource] = []
    k8s_resources: list[K8sResource] = []

    for kust_file in repo_root.rglob("kustomization.yaml"):
        if any(part.startswith(".") for part in kust_file.parts):
            continue
        rel = str(kust_file.relative_to(repo_root))

        try:
            data = yaml.safe_load(kust_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", rel, exc)
            continue

        if not isinstance(data, dict):
            continue

        # Record the kustomization itself
        iac_resources.append(IaCResource(
            source=IaCSource.KUSTOMIZE,
            source_file=rel,
            resource_type="kustomization",
            name=str(kust_file.parent.relative_to(repo_root)),
            provider="kustomize",
            properties={
                "resources": data.get("resources", []),
                "bases": data.get("bases", []),
                "patches": [
                    p if isinstance(p, str) else p.get("path", str(p))
                    for p in data.get("patches", [])
                    if p
                ],
                "namespace": data.get("namespace", ""),
                "generators": list(data.get("generators", [])),
                "transformers": list(data.get("transformers", [])),
            },
        ))

        # Collect Helm chart generators
        for gen in data.get("helmCharts", []):
            if isinstance(gen, dict):
                chart_name = gen.get("name", "")
                archetype, notes = "", []
                for pattern, (arch, n) in _HELM_CHART_ARCHETYPES.items():
                    if pattern in chart_name.lower():
                        archetype, notes = arch, n
                        break
                iac_resources.append(IaCResource(
                    source=IaCSource.KUSTOMIZE,
                    source_file=rel,
                    resource_type="kustomize_helm_chart",
                    name=chart_name,
                    provider="kustomize",
                    properties=gen,
                    archetype=archetype,
                    monitoring_notes=list(notes),
                ))

        # Try `kubectl kustomize`
        rendered = _render_kustomize(kust_file.parent)
        k8s_resources.extend(rendered)

    # Also check for kustomization.yml (alternate extension)
    for kust_file in repo_root.rglob("kustomization.yml"):
        if any(part.startswith(".") for part in kust_file.parts):
            continue
        rel = str(kust_file.relative_to(repo_root))
        try:
            data = yaml.safe_load(kust_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            iac_resources.append(IaCResource(
                source=IaCSource.KUSTOMIZE,
                source_file=rel,
                resource_type="kustomization",
                name=str(kust_file.parent.relative_to(repo_root)),
                provider="kustomize",
                properties={
                    "resources": data.get("resources", []),
                    "bases": data.get("bases", []),
                    "namespace": data.get("namespace", ""),
                },
            ))

    return iac_resources, k8s_resources


def _render_kustomize(kust_dir: Path) -> list[K8sResource]:
    """Try `kubectl kustomize` to render overlays into final manifests."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        logger.debug("kubectl not found — skipping kustomize rendering")
        return []

    try:
        result = subprocess.run(
            [kubectl, "kustomize", str(kust_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("kubectl kustomize failed for %s: %s", kust_dir, result.stderr[:200])
            return []

        resources: list[K8sResource] = []
        for doc in yaml.safe_load_all(result.stdout):
            if doc is None:
                continue
            if isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc and "metadata" in doc:
                from k8s_observability_agent.scanner import _parse_resource
                res = _parse_resource(doc, f"kustomize:{kust_dir.name}")
                resources.append(res)
        return resources

    except Exception as exc:
        logger.debug("kubectl kustomize error: %s", exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  PULUMI PARSER
# ══════════════════════════════════════════════════════════════════════════════


def _discover_pulumi(repo_root: Path) -> list[IaCResource]:
    """Discover Pulumi projects and extract resource definitions via static analysis."""
    resources: list[IaCResource] = []

    for pulumi_yaml in repo_root.rglob("Pulumi.yaml"):
        if any(part.startswith(".") for part in pulumi_yaml.parts):
            continue

        project_dir = pulumi_yaml.parent
        rel_base = str(project_dir.relative_to(repo_root))

        # Parse project metadata
        try:
            proj = yaml.safe_load(pulumi_yaml.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(proj, dict):
            continue

        runtime = proj.get("runtime", "")
        if isinstance(runtime, dict):
            runtime = runtime.get("name", "")
        proj_name = proj.get("name", "unknown")

        resources.append(IaCResource(
            source=IaCSource.PULUMI,
            source_file=f"{rel_base}/Pulumi.yaml",
            resource_type="pulumi_project",
            name=proj_name,
            provider="pulumi",
            properties={"runtime": runtime, "description": proj.get("description", "")},
        ))

        # Parse program files based on runtime
        if runtime in ("python", "python3"):
            program_files = list(project_dir.rglob("*.py"))
        elif runtime in ("nodejs", "typescript"):
            program_files = list(project_dir.rglob("*.ts")) + list(project_dir.rglob("*.js"))
        elif runtime == "go":
            program_files = list(project_dir.rglob("*.go"))
        elif runtime == "yaml":
            program_files = list(project_dir.rglob("*.yaml")) + list(project_dir.rglob("*.yml"))
        else:
            program_files = []

        for prog_file in program_files:
            if any(skip in str(prog_file) for skip in ("node_modules", "venv", ".venv", "__pycache__")):
                continue
            resources.extend(_parse_pulumi_program(prog_file, repo_root, runtime))

    return resources


def _parse_pulumi_program(path: Path, repo_root: Path, runtime: str) -> list[IaCResource]:
    """Static analysis of Pulumi program files to find resource constructors."""
    rel = str(path.relative_to(repo_root))
    resources: list[IaCResource] = []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return resources

    if runtime in ("python", "python3"):
        # Match: aws.rds.Instance("name", ...) or k8s.apps.v1.Deployment("name", ...)
        pattern = re.compile(
            r'(\w+(?:\.\w+)+)\s*\(\s*["\']([^"\']+)["\']',
        )
    elif runtime in ("nodejs", "typescript"):
        # Match: new aws.rds.Instance("name", { ... })
        pattern = re.compile(
            r'new\s+(\w+(?:\.\w+)+)\s*\(\s*["\']([^"\']+)["\']',
        )
    elif runtime == "go":
        # Match: rds.NewInstance(ctx, "name", ...)
        pattern = re.compile(
            r'(\w+)\.New(\w+)\s*\(\s*\w+\s*,\s*["\']([^"\']+)["\']',
        )
    else:
        return resources

    for match in pattern.finditer(text):
        if runtime == "go":
            pkg = match.group(1)
            type_name = match.group(2)
            res_name = match.group(3)
            res_type = f"{pkg}:{type_name}"
        else:
            res_type = match.group(1)
            res_name = match.group(2)

        # Map to archetype — normalise delimiters (Python uses dots, keys use colons)
        archetype, notes = "", []
        normalised = res_type.lower().replace(".", ":").replace("/", ":")
        # Also handle common aliases (k8s → kubernetes)
        normalised_expanded = normalised.replace("k8s:", "kubernetes:")
        for pulumi_type, (arch, n) in _PULUMI_ARCHETYPES.items():
            key = pulumi_type.lower().replace("/", ":")
            if key in normalised or key in normalised_expanded:
                archetype, notes = arch, n
                break

        # Infer provider from the type prefix
        provider = res_type.split(".")[0] if "." in res_type else "unknown"

        resources.append(IaCResource(
            source=IaCSource.PULUMI,
            source_file=rel,
            resource_type=res_type,
            name=res_name,
            provider=provider,
            properties={},
            archetype=archetype,
            monitoring_notes=list(notes),
        ))

    return resources


# ══════════════════════════════════════════════════════════════════════════════
#  TERRAFORM HELM_RELEASE EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════


def _extract_helm_releases_from_terraform(
    tf_resources: list[IaCResource],
) -> list[dict[str, Any]]:
    """Extract helm_release info from parsed Terraform resources."""
    releases: list[dict[str, Any]] = []
    for r in tf_resources:
        if r.resource_type == "helm_release":
            chart = r.properties.get("chart", r.name)
            releases.append({
                "chart": chart,
                "repository": r.properties.get("repository", ""),
                "namespace": r.properties.get("namespace", ""),
                "name": r.name,
                "source": "terraform",
            })
            # Update archetype based on chart name
            for pattern, (arch, notes) in _HELM_CHART_ARCHETYPES.items():
                if pattern in chart.lower():
                    r.archetype = arch
                    r.monitoring_notes = list(notes)
                    break
    return releases


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════


def scan_iac(repo_root: Path) -> IaCDiscovery:
    """Scan a repository for all IaC formats and return aggregated results.

    This is the main entry point for IaC analysis.  It discovers and parses:
    - Terraform (.tf files)
    - Helm Charts (Chart.yaml + values.yaml + templates)
    - Kustomize (kustomization.yaml)
    - Pulumi (Pulumi.yaml + program files)

    Returns an IaCDiscovery with all found resources, helm releases,
    and any K8s resources that could be rendered from IaC.
    """
    repo_root = Path(repo_root).resolve()
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")

    all_resources: list[IaCResource] = []
    all_helm_releases: list[dict[str, Any]] = []
    all_k8s_resources: list[K8sResource] = []
    all_files: list[str] = []
    errors: list[str] = []

    # ── Terraform ─────────────────────────────────────────────────────
    try:
        tf_resources = _discover_terraform(repo_root)
        all_resources.extend(tf_resources)
        all_helm_releases.extend(_extract_helm_releases_from_terraform(tf_resources))
        all_files.extend(sorted({r.source_file for r in tf_resources}))
        logger.info("Terraform: found %d resources in %d files",
                     len(tf_resources), len({r.source_file for r in tf_resources}))
    except Exception as exc:
        errors.append(f"Terraform scan error: {exc}")
        logger.warning("Terraform scan failed: %s", exc)

    # ── Helm ──────────────────────────────────────────────────────────
    try:
        helm_resources, helm_releases, helm_k8s = _discover_helm_charts(repo_root)
        all_resources.extend(helm_resources)
        all_helm_releases.extend(helm_releases)
        all_k8s_resources.extend(helm_k8s)
        all_files.extend(sorted({r.source_file for r in helm_resources}))
        logger.info("Helm: found %d resources, %d releases, %d rendered K8s resources",
                     len(helm_resources), len(helm_releases), len(helm_k8s))
    except Exception as exc:
        errors.append(f"Helm scan error: {exc}")
        logger.warning("Helm scan failed: %s", exc)

    # ── Kustomize ─────────────────────────────────────────────────────
    try:
        kust_resources, kust_k8s = _discover_kustomize(repo_root)
        all_resources.extend(kust_resources)
        all_k8s_resources.extend(kust_k8s)
        all_files.extend(sorted({r.source_file for r in kust_resources}))
        logger.info("Kustomize: found %d resources, %d rendered K8s resources",
                     len(kust_resources), len(kust_k8s))
    except Exception as exc:
        errors.append(f"Kustomize scan error: {exc}")
        logger.warning("Kustomize scan failed: %s", exc)

    # ── Pulumi ────────────────────────────────────────────────────────
    try:
        pulumi_resources = _discover_pulumi(repo_root)
        all_resources.extend(pulumi_resources)
        all_files.extend(sorted({r.source_file for r in pulumi_resources}))
        logger.info("Pulumi: found %d resources", len(pulumi_resources))
    except Exception as exc:
        errors.append(f"Pulumi scan error: {exc}")
        logger.warning("Pulumi scan failed: %s", exc)

    discovery = IaCDiscovery(
        resources=all_resources,
        helm_releases=all_helm_releases,
        k8s_resources_from_iac=all_k8s_resources,
        files_scanned=sorted(set(all_files)),
        errors=errors,
    )

    total = len(all_resources)
    if total:
        logger.info(
            "IaC scan complete: %d resources (%s)",
            total,
            ", ".join(f"{k}={v}" for k, v in discovery.summary().items()),
        )
    else:
        logger.info("IaC scan complete: no IaC resources found")

    return discovery
