"""Tests for agent.analyzer."""

from k8s_observability_agent.analyzer import build_platform, build_relationships, platform_report
from k8s_observability_agent.models import K8sResource


def _make_deployment(
    name: str, namespace: str = "default", labels: dict | None = None
) -> K8sResource:
    labels = labels or {}
    return K8sResource(
        api_version="apps/v1",
        kind="Deployment",
        name=name,
        namespace=namespace,
        labels=labels,
        replicas=2,
        selector=labels,
        raw={
            "spec": {
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {"containers": [{"name": name, "image": f"{name}:latest"}]},
                }
            }
        },
    )


def _make_service(
    name: str, namespace: str = "default", selector: dict | None = None
) -> K8sResource:
    selector = selector or {}
    return K8sResource(
        api_version="v1",
        kind="Service",
        name=name,
        namespace=namespace,
        service_type="ClusterIP",
        selector=selector,
        raw={"spec": {"selector": selector}},
    )


class TestBuildRelationships:
    def test_service_selects_deployment(self) -> None:
        deploy = _make_deployment("web", labels={"app": "web"})
        svc = _make_service("web-svc", selector={"app": "web"})
        rels = build_relationships([deploy, svc])
        assert len(rels) == 1
        assert rels[0].rel_type == "selects"
        assert "Service" in rels[0].source
        assert "Deployment" in rels[0].target

    def test_no_match_when_selector_differs(self) -> None:
        deploy = _make_deployment("web", labels={"app": "web"})
        svc = _make_service("other-svc", selector={"app": "other"})
        rels = build_relationships([deploy, svc])
        assert rels == []


class TestBuildPlatform:
    def test_platform_namespaces(self) -> None:
        resources = [
            K8sResource(kind="Deployment", name="a", namespace="ns1"),
            K8sResource(kind="Service", name="b", namespace="ns2"),
            K8sResource(kind="ConfigMap", name="c", namespace="ns1"),
        ]
        platform = build_platform(resources, ["f1.yaml", "f2.yaml"], [])
        assert platform.namespaces == ["ns1", "ns2"]
        assert len(platform.manifest_files) == 2


class TestPlatformReport:
    def test_report_contains_key_sections(self) -> None:
        deploy = _make_deployment("web", namespace="prod", labels={"app": "web"})
        svc = _make_service("web-svc", namespace="prod", selector={"app": "web"})
        platform = build_platform([deploy, svc], ["web.yaml"], [], repo_path="/repo")
        report = platform_report(platform)
        assert "KUBERNETES PLATFORM SUMMARY" in report
        assert "Deployment" in report
        assert "Service" in report
        assert "WORKLOADS" in report
        assert "SERVICES" in report
