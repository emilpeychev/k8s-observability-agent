"""Tests for agent.scanner."""

from pathlib import Path

from agent.scanner import discover_manifest_files, parse_manifest_file


class TestDiscoverManifestFiles:
    def test_finds_yaml_files(self, tmp_repo: Path) -> None:
        files = discover_manifest_files(tmp_repo)
        names = {f.name for f in files}
        assert "deployment.yaml" in names
        assert "service.yaml" in names
        assert "ingress.yaml" in names

    def test_respects_exclude(self, tmp_repo: Path) -> None:
        # Create a file in an excluded directory
        vendor = tmp_repo / "vendor"
        vendor.mkdir()
        (vendor / "dep.yaml").write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: x\n")
        files = discover_manifest_files(tmp_repo, exclude=["**/vendor/**"])
        assert not any(f.name == "dep.yaml" for f in files)


class TestParseManifestFile:
    def test_parse_deployment(self, tmp_repo: Path) -> None:
        path = tmp_repo / "k8s" / "deployment.yaml"
        resources = parse_manifest_file(path, tmp_repo)
        assert len(resources) == 1
        r = resources[0]
        assert r.kind == "Deployment"
        assert r.name == "web-app"
        assert r.namespace == "production"
        assert r.replicas == 3
        assert len(r.containers) == 1
        assert r.containers[0].name == "nginx"
        assert r.containers[0].image == "nginx:1.25"
        assert 80 in r.containers[0].ports
        assert r.containers[0].liveness_probe
        assert r.containers[0].readiness_probe

    def test_parse_service(self, tmp_repo: Path) -> None:
        path = tmp_repo / "k8s" / "service.yaml"
        resources = parse_manifest_file(path, tmp_repo)
        assert len(resources) == 1
        r = resources[0]
        assert r.kind == "Service"
        assert r.service_type == "ClusterIP"
        assert r.selector == {"app": "web-app"}

    def test_skips_non_k8s(self, tmp_repo: Path) -> None:
        path = tmp_repo / "k8s" / "random.yaml"
        resources = parse_manifest_file(path, tmp_repo)
        assert resources == []

    def test_worker_no_probes(self, tmp_repo: Path) -> None:
        path = tmp_repo / "k8s" / "worker.yaml"
        resources = parse_manifest_file(path, tmp_repo)
        assert len(resources) == 1
        c = resources[0].containers[0]
        assert not c.liveness_probe
        assert not c.readiness_probe
        assert not c.resource_requests
        assert not c.resource_limits

    def test_multi_document_yaml(self, tmp_path: Path) -> None:
        multi = tmp_path / "multi.yaml"
        multi.write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ns1\n"
            "---\n"
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ns2\n"
        )
        resources = parse_manifest_file(multi, tmp_path)
        assert len(resources) == 2
        assert {r.name for r in resources} == {"ns1", "ns2"}
