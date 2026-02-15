"""Tests for agent.scanner."""

from pathlib import Path

from k8s_observability_agent.scanner import discover_manifest_files, parse_manifest_file


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


class TestCapabilityInference:
    """Test the telemetry capability detection in _parse_resource."""

    def test_detects_exporter_sidecar(self, tmp_path: Path) -> None:
        """A postgres_exporter sidecar should be detected."""
        manifest = tmp_path / "pg.yaml"
        manifest.write_text(
            "apiVersion: apps/v1\n"
            "kind: StatefulSet\n"
            "metadata:\n"
            "  name: pg\n"
            "spec:\n"
            "  replicas: 1\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: pg\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: pg\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: postgres\n"
            "          image: postgres:15\n"
            "          ports:\n"
            "            - containerPort: 5432\n"
            "        - name: exporter\n"
            "          image: prometheuscommunity/postgres-exporter:v0.15\n"
            "          ports:\n"
            "            - containerPort: 9187\n"
        )
        resources = parse_manifest_file(manifest, tmp_path)
        assert len(resources) == 1
        r = resources[0]
        assert any("exporter:postgres_exporter" in t for t in r.telemetry)

    def test_no_exporter_means_empty_telemetry(self, tmp_path: Path) -> None:
        """A bare postgres deployment without exporter should have no exporter capability."""
        manifest = tmp_path / "pg-bare.yaml"
        manifest.write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: pg-bare\n"
            "spec:\n"
            "  replicas: 1\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: pg\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: pg\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: postgres\n"
            "          image: postgres:15\n"
        )
        resources = parse_manifest_file(manifest, tmp_path)
        assert len(resources) == 1
        assert not any(t.startswith("exporter:") for t in resources[0].telemetry)

    def test_detects_scrape_annotations(self, tmp_path: Path) -> None:
        """Prometheus scrape annotations should be detected."""
        manifest = tmp_path / "annotated.yaml"
        manifest.write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: annotated-app\n"
            "spec:\n"
            "  replicas: 1\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: myapp\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: myapp\n"
            "      annotations:\n"
            '        prometheus.io/scrape: "true"\n'
            '        prometheus.io/port: "9090"\n'
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          image: myapp:latest\n"
        )
        resources = parse_manifest_file(manifest, tmp_path)
        assert len(resources) == 1
        assert "scrape_annotations" in resources[0].telemetry
        assert "metrics_port:9090" in resources[0].telemetry

    def test_detects_metrics_port_name(self, tmp_path: Path) -> None:
        """A port named 'metrics' should be detected."""
        manifest = tmp_path / "metrics-port.yaml"
        manifest.write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: app-with-metrics\n"
            "spec:\n"
            "  replicas: 1\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: myapp\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: myapp\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          image: myapp:latest\n"
            "          ports:\n"
            "            - name: metrics\n"
            "              containerPort: 8080\n"
        )
        resources = parse_manifest_file(manifest, tmp_path)
        assert len(resources) == 1
        assert "metrics_port:8080" in resources[0].telemetry

    def test_builtin_metrics_profile(self, tmp_path: Path) -> None:
        """Profiles with built-in metrics (e.g., Prometheus) should auto-detect capability."""
        manifest = tmp_path / "prom.yaml"
        manifest.write_text(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: prometheus\n"
            "spec:\n"
            "  replicas: 1\n"
            "  selector:\n"
            "    matchLabels:\n"
            "      app: prometheus\n"
            "  template:\n"
            "    metadata:\n"
            "      labels:\n"
            "        app: prometheus\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: prometheus\n"
            "          image: prom/prometheus:v2.45.0\n"
            "          ports:\n"
            "            - containerPort: 9090\n"
        )
        resources = parse_manifest_file(manifest, tmp_path)
        assert len(resources) == 1
        assert "builtin_metrics" in resources[0].telemetry
