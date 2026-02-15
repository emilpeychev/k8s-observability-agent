"""Tests for agent.classifier — the workload classification engine."""

import pytest

from agent.classifier import (
    ARCHETYPE_CACHE,
    ARCHETYPE_CUSTOM_APP,
    ARCHETYPE_DATABASE,
    ARCHETYPE_LOGGING,
    ARCHETYPE_MESSAGE_QUEUE,
    ARCHETYPE_MONITORING,
    ARCHETYPE_REVERSE_PROXY,
    ARCHETYPE_SEARCH_ENGINE,
    ARCHETYPE_WEB_SERVER,
    all_profiles,
    classify_image,
    get_profile,
)


class TestClassifyImage:
    """Image regex matching — the primary classification path."""

    @pytest.mark.parametrize(
        "image, expected_archetype, expected_display",
        [
            # PostgreSQL variations
            ("postgres:15", ARCHETYPE_DATABASE, "PostgreSQL"),
            ("postgres:15-alpine", ARCHETYPE_DATABASE, "PostgreSQL"),
            ("docker.io/library/postgres:16", ARCHETYPE_DATABASE, "PostgreSQL"),
            ("bitnami/postgresql:15.4", ARCHETYPE_DATABASE, "PostgreSQL"),
            ("registry.example.com/pg-primary:latest", ARCHETYPE_DATABASE, "PostgreSQL"),
            # MySQL / MariaDB
            ("mysql:8.0", ARCHETYPE_DATABASE, "MySQL"),
            ("mariadb:11.1", ARCHETYPE_DATABASE, "MySQL"),
            ("percona:8.0", ARCHETYPE_DATABASE, "MySQL"),
            # MongoDB
            ("mongo:7", ARCHETYPE_DATABASE, "MongoDB"),
            ("mongodb/mongodb-community-server:7.0-ubi8", ARCHETYPE_DATABASE, "MongoDB"),
            # Redis
            ("redis:7-alpine", ARCHETYPE_CACHE, "Redis"),
            ("bitnami/redis:7.2", ARCHETYPE_CACHE, "Redis"),
            ("valkey:8", ARCHETYPE_CACHE, "Redis"),
            ("dragonfly:latest", ARCHETYPE_CACHE, "Redis"),
            # Elasticsearch
            ("elasticsearch:8.11", ARCHETYPE_SEARCH_ENGINE, "Elasticsearch"),
            ("elastic/elasticsearch:8.11.0", ARCHETYPE_SEARCH_ENGINE, "Elasticsearch"),
            ("opensearch:2.11", ARCHETYPE_SEARCH_ENGINE, "Elasticsearch"),
            # Kafka
            ("confluentinc/cp-kafka:7.5", ARCHETYPE_MESSAGE_QUEUE, "Kafka"),
            ("bitnami/kafka:3.6", ARCHETYPE_MESSAGE_QUEUE, "Kafka"),
            # RabbitMQ
            ("rabbitmq:3.12-management", ARCHETYPE_MESSAGE_QUEUE, "RabbitMQ"),
            # NATS
            ("nats:2.10", ARCHETYPE_MESSAGE_QUEUE, "NATS"),
            # NGINX
            ("nginx:1.25", ARCHETYPE_WEB_SERVER, "NGINX"),
            ("nginx:1.25-alpine", ARCHETYPE_WEB_SERVER, "NGINX"),
            # Envoy / Istio
            ("envoyproxy/envoy:v1.28", ARCHETYPE_REVERSE_PROXY, "Envoy"),
            ("istio/proxyv2:1.20", ARCHETYPE_REVERSE_PROXY, "Envoy"),
            # HAProxy
            ("haproxy:2.9", ARCHETYPE_REVERSE_PROXY, "HAProxy"),
            # Traefik
            ("traefik:v3.0", ARCHETYPE_REVERSE_PROXY, "Envoy"),
            # Prometheus
            ("prom/prometheus:v2.48", ARCHETYPE_MONITORING, "Prometheus"),
            # Grafana
            ("grafana/grafana:10.2", ARCHETYPE_MONITORING, "Grafana"),
            # Fluentd / Fluent Bit
            ("fluentd:v1.16", ARCHETYPE_LOGGING, "Fluentd/Fluent Bit"),
            ("fluent/fluent-bit:2.2", ARCHETYPE_LOGGING, "Fluentd/Fluent Bit"),
        ],
    )
    def test_image_classification(
        self, image: str, expected_archetype: str, expected_display: str
    ) -> None:
        result = classify_image(image)
        assert result.archetype == expected_archetype, (
            f"Image '{image}' classified as '{result.archetype}', expected '{expected_archetype}'"
        )
        assert result.confidence == "high"
        assert result.score >= 0.60, f"Image match should be high confidence, got {result.score}"
        assert result.match_source == "image"
        assert result.profile is not None
        assert result.profile.display_name == expected_display
        assert len(result.evidence) >= 1

    def test_custom_app_image_fallback(self) -> None:
        result = classify_image("mycompany/payment-service:v3.2.1")
        assert result.archetype == ARCHETYPE_CUSTOM_APP
        assert result.confidence == "low"
        assert result.score < 0.25
        assert result.match_source == "fallback"
        assert result.profile is None
        assert len(result.evidence) >= 1

    def test_unknown_image_no_crash(self) -> None:
        result = classify_image("")
        assert result.archetype == ARCHETYPE_CUSTOM_APP


class TestPortHeuristics:
    """Fallback classification via well-known ports."""

    @pytest.mark.parametrize(
        "port, expected",
        [
            (5432, ARCHETYPE_DATABASE),
            (3306, ARCHETYPE_DATABASE),
            (6379, ARCHETYPE_CACHE),
            (9200, ARCHETYPE_SEARCH_ENGINE),
            (9092, ARCHETYPE_MESSAGE_QUEUE),
            (5672, ARCHETYPE_MESSAGE_QUEUE),
        ],
    )
    def test_port_classification(self, port: int, expected: str) -> None:
        # Use an unknown image so image match doesn't fire
        result = classify_image("mycompany/unknown:latest", ports=[port])
        assert result.archetype == expected
        assert result.confidence == "medium"
        assert result.match_source == f"port:{port}"


class TestEnvHeuristics:
    """Fallback classification via environment variable names."""

    @pytest.mark.parametrize(
        "env, expected",
        [
            ("POSTGRES_PASSWORD", ARCHETYPE_DATABASE),
            ("MYSQL_ROOT_PASSWORD", ARCHETYPE_DATABASE),
            ("REDIS_PASSWORD", ARCHETYPE_CACHE),
            ("KAFKA_BROKER_ID", ARCHETYPE_MESSAGE_QUEUE),
        ],
    )
    def test_env_classification(self, env: str, expected: str) -> None:
        result = classify_image("mycompany/unknown:latest", env_vars=[env])
        assert result.archetype == expected
        assert result.confidence == "medium"
        assert result.match_source == f"env:{env}"


class TestLabelHeuristics:
    def test_app_kubernetes_io_name(self) -> None:
        result = classify_image(
            "custom-registry.io/db:v1",
            labels={"app.kubernetes.io/name": "postgresql"},
        )
        assert result.archetype == ARCHETYPE_DATABASE
        assert result.confidence == "medium"


class TestPrecedence:
    """Image match should beat port/env matches."""

    def test_image_beats_port(self) -> None:
        # nginx image on port 5432 — image should win
        result = classify_image("nginx:1.25", ports=[5432])
        assert result.archetype == ARCHETYPE_WEB_SERVER

    def test_port_beats_env(self) -> None:
        # Unknown image, Redis port, Postgres env — port should win
        result = classify_image(
            "mycompany/svc:latest",
            ports=[6379],
            env_vars=["POSTGRES_PASSWORD"],
        )
        assert result.archetype == ARCHETYPE_CACHE


class TestEvidenceAccumulation:
    """Verify that multiple signals combine into a higher score."""

    def test_image_only_score(self) -> None:
        result = classify_image("postgres:15")
        assert result.score == 0.70

    def test_image_plus_port_score(self) -> None:
        result = classify_image("postgres:15", ports=[5432])
        assert result.score == 0.95  # 0.70 + 0.25
        assert len(result.evidence) == 2

    def test_image_plus_port_plus_env_score(self) -> None:
        result = classify_image("postgres:15", ports=[5432], env_vars=["POSTGRES_PASSWORD"])
        assert result.score == 1.0  # 0.70 + 0.25 + 0.15 → capped at 1.0
        assert len(result.evidence) == 3

    def test_image_plus_label_score(self) -> None:
        result = classify_image(
            "postgres:15",
            labels={"app.kubernetes.io/name": "postgresql"},
        )
        assert result.score == 0.90  # 0.70 + 0.20

    def test_port_only_score(self) -> None:
        result = classify_image("mycompany/app:v1", ports=[5432])
        assert result.score == 0.25

    def test_env_only_score(self) -> None:
        result = classify_image("mycompany/app:v1", env_vars=["POSTGRES_PASSWORD"])
        assert result.score == 0.15

    def test_port_plus_env_same_profile(self) -> None:
        """Port and env hints for the same technology should combine."""
        result = classify_image(
            "mycompany/app:v1",
            ports=[5432],
            env_vars=["POSTGRES_PASSWORD"],
        )
        assert result.score == 0.40  # 0.25 + 0.15
        assert result.confidence == "medium"
        assert len(result.evidence) == 2

    def test_conflicting_signals_highest_wins(self) -> None:
        """When port says Redis but env says Postgres, highest-score profile wins."""
        result = classify_image(
            "mycompany/app:v1",
            ports=[6379],
            env_vars=["POSTGRES_PASSWORD"],
        )
        # Redis gets 0.25 (port), Postgres gets 0.15 (env) → Redis wins
        assert result.archetype == ARCHETYPE_CACHE
        assert result.score == 0.25

    def test_multiple_env_same_profile_no_double_count(self) -> None:
        """Multiple env vars pointing to the same profile should not inflate score."""
        result = classify_image(
            "mycompany/app:v1",
            env_vars=["POSTGRES_PASSWORD", "POSTGRES_DB", "PGDATA"],
        )
        assert result.score == 0.15  # weighted once, not 3x
        assert result.archetype == ARCHETYPE_DATABASE

    def test_fallback_score(self) -> None:
        result = classify_image("mycompany/payment-service:v3.2.1")
        assert result.score == 0.10
        assert result.confidence == "low"

    def test_evidence_trail_populated(self) -> None:
        result = classify_image(
            "postgres:15",
            ports=[5432],
            env_vars=["POSTGRES_DB"],
        )
        evidence_str = " ".join(result.evidence)
        assert "image:" in evidence_str
        assert "port:5432" in evidence_str
        assert "env:POSTGRES_DB" in evidence_str


class TestProfiles:
    def test_all_profiles_have_golden_metrics(self) -> None:
        for name, profile in all_profiles().items():
            assert len(profile.golden_metrics) > 0, (
                f"Profile '{name}' ({profile.display_name}) has no golden metrics"
            )

    def test_all_profiles_have_alerts(self) -> None:
        for name, profile in all_profiles().items():
            assert len(profile.alerts) > 0, (
                f"Profile '{name}' ({profile.display_name}) has no alerts"
            )

    def test_get_profile_exists(self) -> None:
        assert get_profile("postgresql") is not None
        assert get_profile("redis") is not None
        assert get_profile("kafka") is not None

    def test_get_profile_missing(self) -> None:
        assert get_profile("nonexistent") is None

    def test_alert_expressions_are_nonempty(self) -> None:
        for name, profile in all_profiles().items():
            for alert in profile.alerts:
                assert alert.expr, f"Alert '{alert.name}' in {name} has empty expression"
                assert alert.name, f"Alert in {name} has empty name"
