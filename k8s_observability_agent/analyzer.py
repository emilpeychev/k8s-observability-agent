"""Analyze a scanned Kubernetes platform and derive relationships and insights."""

from __future__ import annotations

import logging

from k8s_observability_agent.models import AwsDiscovery, K8sResource, IaCDiscovery, Platform, ServiceRelationship

logger = logging.getLogger(__name__)


def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """Return True if every key/value in *selector* exists in *labels*."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def build_relationships(resources: list[K8sResource]) -> list[ServiceRelationship]:
    """Infer relationships between K8s resources based on selectors and labels."""
    rels: list[ServiceRelationship] = []

    services = [r for r in resources if r.kind == "Service"]
    workloads = [r for r in resources if r.is_workload]
    ingresses = [r for r in resources if r.kind == "Ingress"]
    hpas = [r for r in resources if r.kind == "HorizontalPodAutoscaler"]

    # Service → Workload (selector match)
    for svc in services:
        if not svc.selector:
            continue
        for wl in workloads:
            pod_labels = (
                wl.raw.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
            )
            if _labels_match(svc.selector, pod_labels):
                rels.append(
                    ServiceRelationship(
                        source=svc.qualified_name,
                        target=wl.qualified_name,
                        rel_type="selects",
                    )
                )

    # Ingress → Service (backend references)
    for ing in ingresses:
        for rule in ing.ingress_rules:
            for path_entry in rule.get("http", {}).get("paths", []):
                backend = path_entry.get("backend", {})
                svc_name = backend.get("service", {}).get("name") or backend.get("serviceName")
                if svc_name:
                    # find matching service in same namespace
                    for svc in services:
                        if svc.name == svc_name and svc.namespace == ing.namespace:
                            rels.append(
                                ServiceRelationship(
                                    source=ing.qualified_name,
                                    target=svc.qualified_name,
                                    rel_type="routes_to",
                                )
                            )

    # HPA → Workload (scaleTargetRef)
    for hpa in hpas:
        target_ref = hpa.raw.get("spec", {}).get("scaleTargetRef", {})
        target_name = target_ref.get("name")
        target_kind = target_ref.get("kind")
        if target_name and target_kind:
            for wl in workloads:
                if (
                    wl.name == target_name
                    and wl.kind == target_kind
                    and wl.namespace == hpa.namespace
                ):
                    rels.append(
                        ServiceRelationship(
                            source=hpa.qualified_name,
                            target=wl.qualified_name,
                            rel_type="scales",
                        )
                    )

    return rels


def build_platform(
    resources: list[K8sResource],
    manifest_files: list[str],
    errors: list[str],
    repo_path: str = "",
    iac_discovery: IaCDiscovery | None = None,
    aws_discovery: AwsDiscovery | None = None,
) -> Platform:
    """Build a complete Platform model from scanned resources."""
    relationships = build_relationships(resources)
    namespaces = sorted({r.namespace for r in resources})

    platform = Platform(
        repo_path=repo_path,
        resources=resources,
        relationships=relationships,
        namespaces=namespaces,
        manifest_files=manifest_files,
        errors=errors,
        iac_discovery=iac_discovery,
        aws_discovery=aws_discovery,
    )
    logger.info(
        "Platform built: %d resources, %d relationships, %d namespaces",
        len(resources),
        len(relationships),
        len(namespaces),
    )
    return platform


def platform_report(platform: Platform) -> str:
    """Return a human-readable text summary of the platform."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("KUBERNETES PLATFORM SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Repository : {platform.repo_path}")
    lines.append(f"Namespaces : {', '.join(platform.namespaces) or '(none)'}")
    lines.append(f"Manifests  : {len(platform.manifest_files)} files")
    lines.append(f"Resources  : {len(platform.resources)} total")
    lines.append("")

    # Resource counts by kind
    summary = platform.summary()
    for kind, count in sorted(summary.items()):
        lines.append(f"  {kind:<30s} {count}")

    # Workload details
    if platform.workloads:
        lines.append("")
        lines.append("-" * 60)
        lines.append("WORKLOADS")
        lines.append("-" * 60)
        for wl in platform.workloads:
            replicas = wl.replicas if wl.replicas is not None else "?"
            lines.append(f"  {wl.qualified_name}  (replicas={replicas})")
            for c in wl.containers:
                probes = []
                if c.liveness_probe:
                    probes.append("liveness")
                if c.readiness_probe:
                    probes.append("readiness")
                if c.startup_probe:
                    probes.append("startup")
                probe_str = ", ".join(probes) if probes else "none"
                arch_str = f"  archetype={c.archetype}"
                if c.archetype_display:
                    arch_str += f" ({c.archetype_display})"
                if c.archetype_confidence != "low":
                    arch_str += f" [{c.archetype_confidence}]"
                lines.append(
                    f"    container: {c.name}  image={c.image}  probes=[{probe_str}]{arch_str}"
                )
            if wl.telemetry:
                lines.append(f"    telemetry: {', '.join(wl.telemetry)}")
            else:
                lines.append("    telemetry: none detected")

    # Service details
    if platform.services:
        lines.append("")
        lines.append("-" * 60)
        lines.append("SERVICES")
        lines.append("-" * 60)
        for svc in platform.services:
            ports = ", ".join(
                f"{p.get('port', '?')}/{p.get('protocol', 'TCP')}" for p in svc.service_ports
            )
            lines.append(f"  {svc.qualified_name}  type={svc.service_type}  ports=[{ports}]")

    # Relationships
    if platform.relationships:
        lines.append("")
        lines.append("-" * 60)
        lines.append("RELATIONSHIPS")
        lines.append("-" * 60)
        for rel in platform.relationships:
            lines.append(f"  {rel.source}  --[{rel.rel_type}]--> {rel.target}")

    # Errors
    if platform.errors:
        lines.append("")
        lines.append("-" * 60)
        lines.append("PARSE ERRORS")
        lines.append("-" * 60)
        for err in platform.errors:
            lines.append(f"  ⚠ {err}")

    # IaC Discovery
    if platform.iac_discovery and platform.iac_discovery.resources:
        iac = platform.iac_discovery
        lines.append("")
        lines.append("-" * 60)
        lines.append("INFRASTRUCTURE AS CODE")
        lines.append("-" * 60)
        for source_name, count in iac.summary().items():
            lines.append(f"  {source_name:<20s} {count} resources")
        lines.append("")
        for r in iac.resources:
            arch_str = f"  [{r.archetype}]" if r.archetype else ""
            lines.append(f"  {r.source.value}:{r.resource_type}/{r.name}{arch_str}")
            for note in r.monitoring_notes:
                lines.append(f"    → {note}")
        if iac.helm_releases:
            lines.append("")
            lines.append("  Helm releases:")
            for hr in iac.helm_releases:
                lines.append(f"    chart={hr.get('chart', '?')}  repo={hr.get('repository', '')}")
        if iac.files_scanned:
            lines.append("")
            lines.append(f"  IaC files scanned: {len(iac.files_scanned)}")

    # AWS Discovery
    if platform.aws_discovery and platform.aws_discovery.resources:
        aws = platform.aws_discovery
        lines.append("")
        lines.append("-" * 60)
        lines.append("AWS LIVE RESOURCES")
        lines.append("-" * 60)
        regions = aws.regions_scanned or ([aws.region] if aws.region else ["(default)"])
        lines.append(f"  Regions scanned: {', '.join(regions)}")
        for rtype, count in sorted(aws.summary().items()):
            lines.append(f"  {rtype:<35s} {count}")
        lines.append("")
        for r in aws.resources:
            status = r.properties.get("status", "")
            status_str = f"  status={status}" if status else ""
            arch_str = f"  [{r.archetype}]" if r.archetype else ""
            lines.append(f"  {r.resource_type}/{r.name}{arch_str}{status_str}")
            for note in r.monitoring_notes:
                lines.append(f"    \u2192 {note}")
        if aws.errors:
            lines.append("")
            for err in aws.errors[:5]:
                lines.append(f"  \u26a0 {err}")

    lines.append("")
    return "\n".join(lines)
