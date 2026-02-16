"""Scan Git repositories for Kubernetes manifest files."""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import pathspec
import yaml
from git import Repo as GitRepo

from k8s_observability_agent.classifier import (
    BUILTIN_METRICS_PROFILES,
    EXPORTER_IMAGE_PATTERNS,
    classify_image,
    get_profile,
)
from k8s_observability_agent.config import Settings
from k8s_observability_agent.iac import scan_iac
from k8s_observability_agent.models import ContainerSpec, IaCDiscovery, K8sResource

logger = logging.getLogger(__name__)

# Kinds we know how to enrich with extra fields.
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
K8S_TOP_LEVEL_KEYS = {"apiVersion", "kind", "metadata"}

# Maximum file size we'll attempt to parse (1 MB). Prevents memory blowup on
# large vendored files, Helm chart archives, terraform state, etc.
MAX_FILE_SIZE_BYTES = 1_048_576  # 1 MB


@contextmanager
def clone_repo(url: str, branch: str = "main") -> Generator[Path, None, None]:
    """Clone a remote Git repository into a temporary directory.

    Yields the clone path, then cleans up the temp directory on exit.
    Uses ``--depth=1 --single-branch`` for speed and disk safety.
    """
    with tempfile.TemporaryDirectory(prefix="k8s-obs-") as tmp:
        tmp_path = Path(tmp)
        logger.info("Cloning %s (branch=%s) → %s", url, branch, tmp_path)
        GitRepo.clone_from(
            url,
            str(tmp_path),
            branch=branch,
            multi_options=["--depth=1", "--single-branch"],
        )
        yield tmp_path


def _build_pathspec(patterns: list[str]) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def discover_manifest_files(
    repo_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Path]:
    """Walk *repo_root* and return paths that look like K8s manifests."""
    include = include or ["**/*.yaml", "**/*.yml", "**/*.json"]
    exclude = exclude or []

    inc_spec = _build_pathspec(include)
    exc_spec = _build_pathspec(exclude) if exclude else None

    candidates: list[Path] = []
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        # Skip files over the size limit — they're almost certainly not K8s manifests.
        try:
            if p.stat().st_size > MAX_FILE_SIZE_BYTES:
                logger.debug("Skipping oversized file (%d bytes): %s", p.stat().st_size, p)
                continue
        except OSError:
            continue
        rel = str(p.relative_to(repo_root))
        if inc_spec.match_file(rel) and (exc_spec is None or not exc_spec.match_file(rel)):
            candidates.append(p)
    return sorted(candidates)


def _looks_like_k8s(doc: dict) -> bool:
    """Heuristic: does this YAML/JSON document look like a K8s manifest?"""
    if not isinstance(doc, dict):
        return False
    return "apiVersion" in doc and "kind" in doc and "metadata" in doc


def _parse_container(raw: dict, labels: dict[str, str] | None = None) -> ContainerSpec:
    """Extract a ContainerSpec from a raw container dict."""
    ports = [p.get("containerPort", 0) for p in raw.get("ports", []) if "containerPort" in p]
    env_names = [e.get("name", "") for e in raw.get("env", [])]
    resources = raw.get("resources", {})
    image = raw.get("image", "")

    # Classify the container image
    classification = classify_image(
        image=image,
        ports=ports,
        env_vars=env_names,
        labels=labels or {},
    )

    return ContainerSpec(
        name=raw.get("name", "unnamed"),
        image=image,
        ports=ports,
        env_vars=env_names,
        resource_requests=resources.get("requests", {}),
        resource_limits=resources.get("limits", {}),
        liveness_probe="livenessProbe" in raw,
        readiness_probe="readinessProbe" in raw,
        startup_probe="startupProbe" in raw,
        archetype=classification.archetype,
        archetype_display=classification.profile.display_name if classification.profile else "",
        archetype_confidence=classification.confidence,
        archetype_score=classification.score,
        archetype_match_source=classification.match_source,
        archetype_evidence=classification.evidence,
    )


def _sanitize_raw(doc: dict) -> dict:
    """Return a copy of *doc* with secret values redacted.

    We keep the structure for relationship/selector analysis but strip
    ``data`` and ``stringData`` from Secret resources so that sensitive
    material never lingers in memory or leaks to the LLM.
    """
    kind = doc.get("kind", "")
    if kind == "Secret":
        sanitized = {k: v for k, v in doc.items() if k not in ("data", "stringData")}
        # Preserve key names (without values) so downstream analysis knows
        # which keys the secret provides.
        for field in ("data", "stringData"):
            if field in doc and isinstance(doc[field], dict):
                sanitized[field] = {k: "REDACTED" for k in doc[field]}
        return sanitized
    return doc


def _detect_telemetry(
    parsed_containers: list[ContainerSpec],
    raw_containers: list[dict],
    pod_annotations: dict[str, str],
) -> list[str]:
    """Infer what telemetry this workload can actually expose.

    This is the capability-inference layer. It checks the pod manifest for
    signals that domain-specific metrics are *collectable*, not just
    *relevant*.  Without this, the tool would emit alerts referencing
    metrics that exist only if an exporter sidecar is deployed.

    Detected capabilities (stored as plain strings):
    * ``"exporter:<name>"``      — an exporter sidecar container is present
    * ``"builtin_metrics"``      — the profile itself exposes /metrics
    * ``"metrics_port:<port>"``  — a container port named "metrics" exists
    * ``"scrape_annotations"``   — ``prometheus.io/scrape: "true"`` is set
    """
    caps: list[str] = []

    all_images = [c.get("image", "") for c in raw_containers]

    # 1. Exporter sidecar detection — match container images against known
    #    exporter patterns.
    for exporter_name, pattern in EXPORTER_IMAGE_PATTERNS.items():
        for img in all_images:
            if pattern.search(img):
                caps.append(f"exporter:{exporter_name}")
                break  # one match per exporter is enough

    # 2. Built-in metrics — profiles like Envoy, Prometheus, Grafana expose
    #    /metrics from the main container.  If ANY container was classified
    #    into a built-in profile, record the capability.
    for c in parsed_containers:
        if c.archetype != "custom-app" and c.archetype_display:
            profile_key = c.archetype_display.lower().replace(" ", "_").replace("/", "_")
            if profile_key in BUILTIN_METRICS_PROFILES:
                caps.append("builtin_metrics")
                # Also record as exporter so `requires: "exporter"` passes
                profile = get_profile(profile_key)
                if profile and profile.exporter:
                    caps.append(f"exporter:{profile.exporter}")

    # 3. Ports named "metrics" — a strong signal that something exposes
    #    Prometheus metrics, even if we can't identify the specific exporter.
    for raw in raw_containers:
        for port in raw.get("ports", []):
            port_name = port.get("name", "").lower()
            if port_name == "metrics":
                cp = port.get("containerPort", 0)
                caps.append(f"metrics_port:{cp}")

    # 4. Prometheus scrape annotations on the pod template.
    scrape = pod_annotations.get("prometheus.io/scrape", "").lower()
    if scrape == "true":
        caps.append("scrape_annotations")
        scrape_port = pod_annotations.get("prometheus.io/port", "")
        if scrape_port:
            caps.append(f"metrics_port:{scrape_port}")

    return caps


def _parse_resource(doc: dict, source_file: str) -> K8sResource:
    """Convert a raw manifest dict into a K8sResource model."""
    metadata = doc.get("metadata", {})
    spec = doc.get("spec", {})

    res = K8sResource(
        api_version=doc.get("apiVersion", ""),
        kind=doc.get("kind", "Unknown"),
        name=metadata.get("name", "unnamed"),
        namespace=metadata.get("namespace", "default"),
        labels=metadata.get("labels", {}),
        annotations=metadata.get("annotations", {}),
        source_file=source_file,
        raw=_sanitize_raw(doc),
    )

    # Enrich workloads
    if res.kind in WORKLOAD_KINDS:
        res.replicas = spec.get("replicas")
        # Pod template may be nested under spec.template.spec or spec.jobTemplate.template.spec
        pod_spec = spec.get("template", {}).get("spec", {})
        if not pod_spec and res.kind == "CronJob":
            pod_spec = (
                spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
            )
        pod_labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        res.containers = [
            _parse_container(c, labels=pod_labels) for c in pod_spec.get("containers", [])
        ]
        match_labels = spec.get("selector", {}).get("matchLabels", {})
        res.selector = match_labels

        # ── Capability inference ──────────────────────────────────────
        # Detect what telemetry this workload can actually produce.
        pod_annotations = spec.get("template", {}).get("metadata", {}).get("annotations", {})
        raw_containers = pod_spec.get("containers", []) + pod_spec.get("initContainers", [])
        res.telemetry = _detect_telemetry(res.containers, raw_containers, pod_annotations)

    # Enrich services
    if res.kind == "Service":
        res.service_type = spec.get("type", "ClusterIP")
        res.service_ports = spec.get("ports", [])
        res.selector = spec.get("selector", {})

    # Enrich ingresses
    if res.kind == "Ingress":
        res.ingress_rules = spec.get("rules", [])

    return res


def parse_manifest_file(path: Path, repo_root: Path | None = None) -> list[K8sResource]:
    """Parse a YAML/JSON file and return all K8s resources found inside."""
    rel = str(path.relative_to(repo_root)) if repo_root else str(path)
    resources: list[K8sResource] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Handle multi-document YAML
        docs = list(yaml.safe_load_all(text))
        for doc in docs:
            if doc is None:
                continue
            # Handle List kind
            if isinstance(doc, dict) and doc.get("kind") == "List":
                for item in doc.get("items", []):
                    if _looks_like_k8s(item):
                        resources.append(_parse_resource(item, rel))
            elif _looks_like_k8s(doc):
                resources.append(_parse_resource(doc, rel))
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", rel, exc)
    return resources


def scan_repository(settings: Settings) -> tuple[list[K8sResource], list[str], list[str], IaCDiscovery | None]:
    """Scan a repository and return (resources, manifest_files, errors, iac_discovery).

    If *settings.github_url* is set the repo is cloned first into a temp
    directory that is automatically cleaned up after scanning.
    """
    if settings.github_url:
        with clone_repo(settings.github_url, settings.branch) as repo_root:
            return _scan_directory(repo_root, settings)
    else:
        repo_root = Path(settings.repo_path).resolve()
        if not repo_root.is_dir():
            raise FileNotFoundError(f"Repository path does not exist: {repo_root}")
        return _scan_directory(repo_root, settings)


def _scan_directory(
    repo_root: Path,
    settings: Settings,
) -> tuple[list[K8sResource], list[str], list[str], IaCDiscovery | None]:
    """Internal: scan a directory after it's been resolved/cloned."""
    if not repo_root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo_root}")

    manifest_files = discover_manifest_files(
        repo_root,
        include=settings.include_patterns,
        exclude=settings.exclude_patterns,
    )

    all_resources: list[K8sResource] = []
    errors: list[str] = []
    file_paths: list[str] = []

    for mf in manifest_files:
        rel = str(mf.relative_to(repo_root))
        try:
            parsed = parse_manifest_file(mf, repo_root)
            if parsed:
                file_paths.append(rel)
                all_resources.extend(parsed)
        except Exception as exc:
            errors.append(f"{rel}: {exc}")

    # ── IaC scanning ──────────────────────────────────────────────────
    iac_discovery: IaCDiscovery | None = None
    try:
        iac_discovery = scan_iac(repo_root)
        # Merge rendered K8s resources from IaC into the main resource list
        if iac_discovery.k8s_resources_from_iac:
            all_resources.extend(iac_discovery.k8s_resources_from_iac)
            logger.info(
                "Added %d K8s resources rendered from IaC",
                len(iac_discovery.k8s_resources_from_iac),
            )
        if iac_discovery.errors:
            errors.extend(iac_discovery.errors)
    except Exception as exc:
        logger.warning("IaC scanning failed: %s", exc)
        errors.append(f"IaC scan error: {exc}")

    logger.info(
        "Scanned %d files → %d K8s resources (%d errors)",
        len(manifest_files),
        len(all_resources),
        len(errors),
    )
    return all_resources, file_paths, errors, iac_discovery
