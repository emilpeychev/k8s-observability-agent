"""Tests for IaC analysis — Terraform, Helm, Kustomize, and Pulumi parsers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from k8s_observability_agent.iac import (
    _discover_helm_charts,
    _discover_kustomize,
    _discover_pulumi,
    _discover_terraform,
    _extract_tf_block_props,
    _find_images_in_dict,
    _parse_terraform_regex,
    scan_iac,
)
from k8s_observability_agent.models import IaCDiscovery, IaCResource, IaCSource


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures — tiny repos with IaC files
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def tf_repo(tmp_path: Path) -> Path:
    """Repository with Terraform files."""
    (tmp_path / "main.tf").write_text(
        textwrap.dedent("""\
        resource "aws_db_instance" "primary" {
          engine         = "postgres"
          engine_version = "15.4"
          instance_class = "db.t3.micro"
          allocated_storage = 20
          name           = "mydb"
        }

        resource "aws_elasticache_cluster" "redis" {
          engine         = "redis"
          node_type      = "cache.t3.micro"
        }

        resource "aws_s3_bucket" "assets" {
          bucket = "my-assets"
        }
        """)
    )
    (tmp_path / "network.tf").write_text(
        textwrap.dedent("""\
        resource "aws_vpc" "main" {
          cidr_block = "10.0.0.0/16"
        }
        """)
    )
    return tmp_path


@pytest.fixture()
def helm_repo(tmp_path: Path) -> Path:
    """Repository with Helm chart."""
    chart_dir = tmp_path / "charts" / "my-app"
    chart_dir.mkdir(parents=True)

    (chart_dir / "Chart.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: v2
        name: my-app
        version: 1.0.0
        appVersion: "2.3.0"
        description: My application chart
        type: application
        dependencies:
          - name: postgresql
            version: "12.0.0"
            repository: https://charts.bitnami.com/bitnami
          - name: redis
            version: "17.0.0"
            repository: https://charts.bitnami.com/bitnami
        """)
    )

    (chart_dir / "values.yaml").write_text(
        textwrap.dedent("""\
        replicaCount: 3
        image:
          repository: myregistry.io/my-app
          tag: "2.3.0"
        sidecar:
          image: myregistry.io/sidecar:1.0
        nested:
          deep:
            image:
              repository: ghcr.io/org/helper
              tag: latest
        """)
    )
    return tmp_path


@pytest.fixture()
def kustomize_repo(tmp_path: Path) -> Path:
    """Repository with kustomization.yaml."""
    base = tmp_path / "k8s" / "base"
    base.mkdir(parents=True)

    (base / "kustomization.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: kustomize.config.k8s.io/v1beta1
        kind: Kustomization
        namespace: production
        resources:
          - deployment.yaml
          - service.yaml
        patches:
          - path: patch-replicas.yaml
        helmCharts:
          - name: nginx-ingress
            repo: https://charts.helm.sh/stable
            version: "4.0.0"
        """)
    )
    return tmp_path


@pytest.fixture()
def pulumi_repo(tmp_path: Path) -> Path:
    """Repository with Pulumi project (Python runtime)."""
    proj_dir = tmp_path / "infra"
    proj_dir.mkdir()

    (proj_dir / "Pulumi.yaml").write_text(
        textwrap.dedent("""\
        name: my-platform
        runtime: python
        description: My infrastructure project
        """)
    )

    (proj_dir / "__main__.py").write_text(
        textwrap.dedent("""\
        import pulumi_aws as aws
        import pulumi_kubernetes as k8s

        db = aws.rds.Instance("primary-db",
            engine="postgres",
            instance_class="db.t3.micro",
        )

        cache = aws.elasticache.Cluster("cache",
            engine="redis",
        )

        deployment = k8s.apps.v1.Deployment("web",
            metadata={"name": "web-app"},
        )
        """)
    )
    return tmp_path


@pytest.fixture()
def mixed_repo(tmp_path: Path) -> Path:
    """Repository mixing Terraform + Helm + Kustomize."""
    # Terraform
    (tmp_path / "infra.tf").write_text(
        textwrap.dedent("""\
        resource "aws_db_instance" "main" {
          engine = "postgres"
        }
        """)
    )

    # Helm chart
    chart = tmp_path / "charts" / "api"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: v2
        name: api
        version: 0.1.0
        """)
    )

    # Kustomize
    deploy = tmp_path / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "kustomization.yaml").write_text(
        textwrap.dedent("""\
        resources:
          - ../base
        """)
    )

    return tmp_path


# ══════════════════════════════════════════════════════════════════════════════
#  Terraform Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestTerraform:
    def test_discover_finds_all_resources(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        names = {r.name for r in resources}
        assert "primary" in names
        assert "redis" in names
        assert "assets" in names
        assert "main" in names  # VPC

    def test_resource_types_parsed(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        types = {r.resource_type for r in resources}
        assert "aws_db_instance" in types
        assert "aws_elasticache_cluster" in types
        assert "aws_s3_bucket" in types
        assert "aws_vpc" in types

    def test_archetype_mapping(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        by_name = {r.name: r for r in resources}
        assert by_name["primary"].archetype == "database"
        assert by_name["redis"].archetype == "cache"
        assert by_name["assets"].archetype == "custom-app"  # s3 bucket

    def test_provider_extracted(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        assert all(r.provider == "aws" for r in resources)

    def test_properties_extracted(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        db = next(r for r in resources if r.name == "primary")
        assert db.properties.get("engine") == "postgres"

    def test_source_is_terraform(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        assert all(r.source == IaCSource.TERRAFORM for r in resources)

    def test_source_file_relative(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        for r in resources:
            assert not r.source_file.startswith("/")
            assert r.source_file.endswith(".tf")

    def test_monitoring_notes_present(self, tf_repo: Path) -> None:
        resources = _discover_terraform(tf_repo)
        db = next(r for r in resources if r.name == "primary")
        assert len(db.monitoring_notes) > 0
        # Should mention postgres_exporter or CloudWatch
        assert any("exporter" in n.lower() or "cloudwatch" in n.lower() for n in db.monitoring_notes)

    def test_skips_dotdirs(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".terraform" / "modules"
        hidden.mkdir(parents=True)
        (hidden / "provider.tf").write_text('resource "aws_s3_bucket" "x" {}')
        resources = _discover_terraform(tmp_path)
        assert len(resources) == 0

    def test_regex_fallback(self, tf_repo: Path) -> None:
        """The regex parser should also find resources."""
        main_tf = tf_repo / "main.tf"
        resources = _parse_terraform_regex(main_tf, tf_repo)
        names = {r.name for r in resources}
        assert "primary" in names
        assert "redis" in names

    def test_extract_tf_block_props(self) -> None:
        block = '''
          engine         = "postgres"
          engine_version = "15.4"
          instance_class = "db.t3.micro"
          tags = {
            Name = "mydb"
          }
        }
        '''
        props = _extract_tf_block_props(block, 0)
        assert props.get("engine") == "postgres"
        assert props.get("engine_version") == "15.4"
        assert props.get("instance_class") == "db.t3.micro"

    def test_empty_repo(self, tmp_path: Path) -> None:
        resources = _discover_terraform(tmp_path)
        assert resources == []


# ══════════════════════════════════════════════════════════════════════════════
#  Helm Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestHelm:
    def test_discover_chart(self, helm_repo: Path) -> None:
        resources, releases, _k8s = _discover_helm_charts(helm_repo)
        chart_resources = [r for r in resources if r.resource_type == "helm_chart"]
        assert len(chart_resources) == 1
        assert chart_resources[0].name == "my-app"

    def test_chart_properties(self, helm_repo: Path) -> None:
        resources, _, _ = _discover_helm_charts(helm_repo)
        chart = next(r for r in resources if r.resource_type == "helm_chart")
        assert chart.properties["version"] == "1.0.0"
        assert chart.properties["app_version"] == "2.3.0"
        assert chart.properties["type"] == "application"

    def test_dependencies_as_releases(self, helm_repo: Path) -> None:
        _, releases, _ = _discover_helm_charts(helm_repo)
        chart_names = [r["chart"] for r in releases]
        assert "postgresql" in chart_names
        assert "redis" in chart_names

    def test_dependency_archetype_mapping(self, helm_repo: Path) -> None:
        resources, _, _ = _discover_helm_charts(helm_repo)
        deps = [r for r in resources if r.resource_type == "helm_dependency"]
        assert len(deps) == 2
        by_name = {d.name: d for d in deps}
        assert by_name["postgresql"].archetype == "database"
        assert by_name["redis"].archetype == "cache"

    def test_image_refs_extracted(self, helm_repo: Path) -> None:
        resources, _, _ = _discover_helm_charts(helm_repo)
        image_refs = [r for r in resources if r.resource_type == "helm_image_ref"]
        images = [r.properties.get("image", "") for r in image_refs]
        assert any("myregistry.io/my-app:2.3.0" in img for img in images)
        assert any("ghcr.io/org/helper" in img for img in images)

    def test_source_is_helm(self, helm_repo: Path) -> None:
        resources, _, _ = _discover_helm_charts(helm_repo)
        assert all(r.source == IaCSource.HELM for r in resources)

    def test_empty_repo(self, tmp_path: Path) -> None:
        resources, releases, k8s = _discover_helm_charts(tmp_path)
        assert resources == []
        assert releases == []
        assert k8s == []


class TestFindImagesInDict:
    def test_repository_tag_pattern(self) -> None:
        data = {"image": {"repository": "nginx", "tag": "1.25"}}
        images = _find_images_in_dict(data)
        assert "nginx:1.25" in images

    def test_string_image(self) -> None:
        data = {"image": "docker.io/library/nginx:latest"}
        images = _find_images_in_dict(data)
        assert "docker.io/library/nginx:latest" in images

    def test_nested_images(self) -> None:
        data = {
            "proxy": {"image": {"repository": "envoy", "tag": "v1.0"}},
            "app": {"image": {"repository": "myapp", "tag": "2.0"}},
        }
        images = _find_images_in_dict(data)
        assert "envoy:v1.0" in images
        assert "myapp:2.0" in images

    def test_no_images(self) -> None:
        data = {"replicas": 3, "port": 8080}
        images = _find_images_in_dict(data)
        assert images == []

    def test_repository_without_tag(self) -> None:
        data = {"image": {"repository": "nginx", "tag": ""}}
        images = _find_images_in_dict(data)
        assert "nginx" in images


# ══════════════════════════════════════════════════════════════════════════════
#  Kustomize Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestKustomize:
    def test_discover_kustomization(self, kustomize_repo: Path) -> None:
        resources, _k8s = _discover_kustomize(kustomize_repo)
        kust = [r for r in resources if r.resource_type == "kustomization"]
        assert len(kust) == 1

    def test_kustomization_properties(self, kustomize_repo: Path) -> None:
        resources, _ = _discover_kustomize(kustomize_repo)
        kust = next(r for r in resources if r.resource_type == "kustomization")
        assert kust.properties["namespace"] == "production"
        assert "deployment.yaml" in kust.properties["resources"]
        assert "service.yaml" in kust.properties["resources"]
        assert "patch-replicas.yaml" in kust.properties["patches"]

    def test_helm_chart_generators(self, kustomize_repo: Path) -> None:
        resources, _ = _discover_kustomize(kustomize_repo)
        helm_charts = [r for r in resources if r.resource_type == "kustomize_helm_chart"]
        assert len(helm_charts) == 1
        assert helm_charts[0].name == "nginx-ingress"

    def test_source_is_kustomize(self, kustomize_repo: Path) -> None:
        resources, _ = _discover_kustomize(kustomize_repo)
        assert all(r.source == IaCSource.KUSTOMIZE for r in resources)

    def test_empty_repo(self, tmp_path: Path) -> None:
        resources, k8s = _discover_kustomize(tmp_path)
        assert resources == []
        assert k8s == []


# ══════════════════════════════════════════════════════════════════════════════
#  Pulumi Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestPulumi:
    def test_discover_project(self, pulumi_repo: Path) -> None:
        resources = _discover_pulumi(pulumi_repo)
        projects = [r for r in resources if r.resource_type == "pulumi_project"]
        assert len(projects) == 1
        assert projects[0].name == "my-platform"
        assert projects[0].properties["runtime"] == "python"

    def test_discover_resources(self, pulumi_repo: Path) -> None:
        resources = _discover_pulumi(pulumi_repo)
        # Exclude project metadata resource
        infra = [r for r in resources if r.resource_type != "pulumi_project"]
        names = {r.name for r in infra}
        assert "primary-db" in names
        assert "cache" in names
        assert "web" in names

    def test_archetype_mapping(self, pulumi_repo: Path) -> None:
        resources = _discover_pulumi(pulumi_repo)
        infra = {r.name: r for r in resources if r.resource_type != "pulumi_project"}
        assert infra["primary-db"].archetype == "database"
        assert infra["cache"].archetype == "cache"
        assert infra["web"].archetype == "custom-app"

    def test_source_is_pulumi(self, pulumi_repo: Path) -> None:
        resources = _discover_pulumi(pulumi_repo)
        assert all(r.source == IaCSource.PULUMI for r in resources)

    def test_empty_repo(self, tmp_path: Path) -> None:
        resources = _discover_pulumi(tmp_path)
        assert resources == []

    def test_nodejs_runtime(self, tmp_path: Path) -> None:
        proj_dir = tmp_path / "infra"
        proj_dir.mkdir()
        (proj_dir / "Pulumi.yaml").write_text("name: ts-proj\nruntime: nodejs\n")
        (proj_dir / "index.ts").write_text(
            textwrap.dedent("""\
            import * as aws from "@pulumi/aws";
            const db = new aws.rds.Instance("my-rds", {
                engine: "postgres",
            });
            """)
        )
        resources = _discover_pulumi(tmp_path)
        infra = [r for r in resources if r.resource_type != "pulumi_project"]
        assert len(infra) == 1
        assert infra[0].name == "my-rds"
        assert infra[0].archetype == "database"

    def test_go_runtime(self, tmp_path: Path) -> None:
        proj_dir = tmp_path / "infra"
        proj_dir.mkdir()
        (proj_dir / "Pulumi.yaml").write_text("name: go-proj\nruntime: go\n")
        (proj_dir / "main.go").write_text(
            textwrap.dedent("""\
            package main
            import "github.com/pulumi/pulumi-aws/sdk/v5/go/aws/rds"
            func main() {
                rds.NewInstance(ctx, "go-db", &rds.InstanceArgs{})
            }
            """)
        )
        resources = _discover_pulumi(tmp_path)
        infra = [r for r in resources if r.resource_type != "pulumi_project"]
        assert len(infra) == 1
        assert infra[0].name == "go-db"


# ══════════════════════════════════════════════════════════════════════════════
#  scan_iac Integration Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestScanIaC:
    def test_empty_repo(self, tmp_path: Path) -> None:
        discovery = scan_iac(tmp_path)
        assert isinstance(discovery, IaCDiscovery)
        assert len(discovery.resources) == 0
        assert not discovery.has_terraform
        assert not discovery.has_helm
        assert not discovery.has_kustomize
        assert not discovery.has_pulumi

    def test_terraform_only(self, tf_repo: Path) -> None:
        discovery = scan_iac(tf_repo)
        assert discovery.has_terraform
        assert not discovery.has_helm
        assert discovery.summary()["terraform"] == 4  # 3 in main.tf + 1 in network.tf

    def test_helm_only(self, helm_repo: Path) -> None:
        discovery = scan_iac(helm_repo)
        assert discovery.has_helm
        assert not discovery.has_terraform
        assert len(discovery.helm_releases) == 2  # postgresql, redis

    def test_kustomize_only(self, kustomize_repo: Path) -> None:
        discovery = scan_iac(kustomize_repo)
        assert discovery.has_kustomize

    def test_pulumi_only(self, pulumi_repo: Path) -> None:
        discovery = scan_iac(pulumi_repo)
        assert discovery.has_pulumi

    def test_mixed_repo(self, mixed_repo: Path) -> None:
        discovery = scan_iac(mixed_repo)
        assert discovery.has_terraform
        assert discovery.has_helm
        assert discovery.has_kustomize

    def test_summary_counts(self, mixed_repo: Path) -> None:
        discovery = scan_iac(mixed_repo)
        summary = discovery.summary()
        assert "terraform" in summary
        assert "helm" in summary
        assert "kustomize" in summary

    def test_files_scanned_populated(self, tf_repo: Path) -> None:
        discovery = scan_iac(tf_repo)
        assert len(discovery.files_scanned) > 0
        assert all(f.endswith(".tf") for f in discovery.files_scanned)

    def test_nonexistent_path_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            scan_iac(Path("/nonexistent/path"))


# ══════════════════════════════════════════════════════════════════════════════
#  IaCDiscovery Model Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestIaCDiscoveryModel:
    def test_empty_discovery(self) -> None:
        d = IaCDiscovery()
        assert not d.has_terraform
        assert not d.has_helm
        assert not d.has_kustomize
        assert not d.has_pulumi
        assert d.summary() == {}

    def test_has_flags(self) -> None:
        d = IaCDiscovery(
            resources=[
                IaCResource(
                    source=IaCSource.TERRAFORM,
                    resource_type="aws_db_instance",
                    name="db",
                ),
                IaCResource(
                    source=IaCSource.HELM,
                    resource_type="helm_chart",
                    name="app",
                ),
            ]
        )
        assert d.has_terraform
        assert d.has_helm
        assert not d.has_kustomize
        assert not d.has_pulumi

    def test_summary_counts_by_source(self) -> None:
        d = IaCDiscovery(
            resources=[
                IaCResource(source=IaCSource.TERRAFORM, resource_type="a", name="x"),
                IaCResource(source=IaCSource.TERRAFORM, resource_type="b", name="y"),
                IaCResource(source=IaCSource.HELM, resource_type="c", name="z"),
            ]
        )
        assert d.summary() == {"terraform": 2, "helm": 1}

    def test_has_helm_from_releases(self) -> None:
        d = IaCDiscovery(helm_releases=[{"chart": "redis"}])
        assert d.has_helm

    def test_display_type(self) -> None:
        r = IaCResource(
            source=IaCSource.TERRAFORM,
            resource_type="aws_db_instance",
            name="x",
        )
        assert r.display_type == "Aws Db Instance"


# ══════════════════════════════════════════════════════════════════════════════
#  Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_malformed_tf_file(self, tmp_path: Path) -> None:
        (tmp_path / "bad.tf").write_text("this is not valid terraform {{{")
        resources = _discover_terraform(tmp_path)
        # Regex fallback should still work (no resource blocks found)
        assert len(resources) == 0

    def test_malformed_chart_yaml(self, tmp_path: Path) -> None:
        chart_dir = tmp_path / "charts" / "bad"
        chart_dir.mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text("not: valid: yaml: [[[")
        resources, releases, k8s = _discover_helm_charts(tmp_path)
        # Should not crash
        assert isinstance(resources, list)

    def test_malformed_kustomization(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yaml").write_text("null")
        resources, k8s = _discover_kustomize(tmp_path)
        assert isinstance(resources, list)

    def test_malformed_pulumi_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "Pulumi.yaml").write_text("null")
        resources = _discover_pulumi(tmp_path)
        assert isinstance(resources, list)

    def test_binary_tf_file(self, tmp_path: Path) -> None:
        (tmp_path / "binary.tf").write_bytes(b"\x00\x01\x02\x03")
        resources = _discover_terraform(tmp_path)
        # Should not crash
        assert isinstance(resources, list)

    def test_empty_values_yaml(self, tmp_path: Path) -> None:
        chart_dir = tmp_path / "charts" / "empty"
        chart_dir.mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: empty\nversion: 1.0.0\n")
        (chart_dir / "values.yaml").write_text("")
        resources, _, _ = _discover_helm_charts(tmp_path)
        assert len(resources) == 1  # just the chart resource
