"""Classify container images into workload archetypes with domain-specific observability knowledge.

This is the core intelligence layer. Without it, every Deployment gets the same
generic 'high CPU / pod restarts' alerts. With it, a PostgreSQL StatefulSet gets
replication-lag alerts, connection-pool saturation metrics, and WAL archive
dashboard panels — while an nginx Deployment gets upstream latency percentiles
and active-connection gauges.

Classification is deterministic (no LLM call). It runs during scanning so the
agent already has archetype context before it even starts reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ──────────────────────────── Archetype Enum ──────────────────────────────────

# Intentionally *not* an Enum so new archetypes can be added without code
# changes. A plain string with constants is more extensible.

ARCHETYPE_DATABASE = "database"
ARCHETYPE_CACHE = "cache"
ARCHETYPE_MESSAGE_QUEUE = "message-queue"
ARCHETYPE_SEARCH_ENGINE = "search-engine"
ARCHETYPE_WEB_SERVER = "web-server"
ARCHETYPE_REVERSE_PROXY = "reverse-proxy"
ARCHETYPE_API_GATEWAY = "api-gateway"
ARCHETYPE_MONITORING = "monitoring"
ARCHETYPE_LOGGING = "logging"
ARCHETYPE_CUSTOM_APP = "custom-app"


# ──────────────────────────── Observability Signals ───────────────────────────


@dataclass(frozen=True)
class MetricSignal:
    """A specific metric an operator should collect for this archetype."""

    name: str
    query: str
    description: str
    panel_type: str = "timeseries"
    requires: str = ""  # deployment prerequisite, e.g. "replicas>1", "statefulset"


@dataclass(frozen=True)
class AlertSignal:
    """A specific alert an operator should configure for this archetype."""

    name: str
    expr: str
    severity: str = "warning"
    for_duration: str = "5m"
    summary: str = ""
    requires: str = ""  # deployment prerequisite, e.g. "replicas>1", "statefulset"


@dataclass(frozen=True)
class ArchetypeProfile:
    """Everything the agent needs to know about a workload archetype to produce
    intelligent, domain-specific observability recommendations."""

    archetype: str
    display_name: str
    description: str
    key: str = ""  # unique registry key, e.g. "postgresql", "mysql"
    exporter: str = ""  # e.g. "postgres_exporter", "redis_exporter"
    exporter_port: int = 0
    golden_metrics: list[MetricSignal] = field(default_factory=list)
    alerts: list[AlertSignal] = field(default_factory=list)
    dashboard_tags: list[str] = field(default_factory=list)
    health_requirements: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# ──────────────────────────── Archetype Catalog ───────────────────────────────

_PROFILES: dict[str, ArchetypeProfile] = {}


def _register(p: ArchetypeProfile) -> ArchetypeProfile:
    registry_key = p.key or p.display_name.lower().replace(" ", "_").replace("/", "_")
    # Store by unique key so profiles with the same archetype don't overwrite each other
    _PROFILES[registry_key] = p
    return p


# ── PostgreSQL ────────────────────────────────────────────────────────────────

_PG_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_DATABASE,
        display_name="PostgreSQL",
        description="Relational database — monitor connections, replication, query performance, and WAL archiving.",
        exporter="postgres_exporter",
        exporter_port=9187,
        golden_metrics=[
            MetricSignal(
                "pg_active_connections",
                'pg_stat_activity_count{state="active"}',
                "Active connections (should stay below max_connections)",
                requires="exporter",
            ),
            MetricSignal(
                "pg_replication_lag_bytes",
                "pg_replication_lag_bytes",
                "Replication lag in bytes (streaming replicas)",
                requires="exporter,replicas>1",
            ),
            MetricSignal(
                "pg_transactions_per_sec",
                "rate(pg_stat_database_xact_commit[5m]) + rate(pg_stat_database_xact_rollback[5m])",
                "Transaction throughput (commits + rollbacks)",
                requires="exporter",
            ),
            MetricSignal(
                "pg_cache_hit_ratio",
                "pg_stat_database_blks_hit / (pg_stat_database_blks_hit + pg_stat_database_blks_read)",
                "Buffer cache hit ratio (should be > 0.99)",
                panel_type="gauge",
                requires="exporter",
            ),
            MetricSignal(
                "pg_dead_tuples",
                "pg_stat_user_tables_n_dead_tup",
                "Dead tuples awaiting vacuum",
                requires="exporter",
            ),
        ],
        alerts=[
            AlertSignal(
                "PostgresTooManyConnections",
                "pg_stat_activity_count > (pg_settings_max_connections * 0.8)",
                severity="warning",
                for_duration="5m",
                summary="PostgreSQL connection count exceeds 80% of max_connections",
                requires="exporter",
            ),
            AlertSignal(
                "PostgresReplicationLagHigh",
                "pg_replication_lag_bytes > 100 * 1024 * 1024",
                severity="critical",
                for_duration="5m",
                summary="PostgreSQL replication lag exceeds 100 MB",
                requires="exporter,replicas>1",
            ),
            AlertSignal(
                "PostgresDeadTuplesHigh",
                "pg_stat_user_tables_n_dead_tup > 10000",
                severity="warning",
                for_duration="15m",
                summary="High dead tuple count — autovacuum may be falling behind",
                requires="exporter",
            ),
            AlertSignal(
                "PostgresCacheHitRatioLow",
                "(pg_stat_database_blks_hit / (pg_stat_database_blks_hit + pg_stat_database_blks_read)) < 0.95",
                severity="warning",
                for_duration="10m",
                summary="Buffer cache hit ratio below 95% — consider increasing shared_buffers",
                requires="exporter",
            ),
        ],
        dashboard_tags=["postgresql", "database"],
        health_requirements=[
            "Deploy postgres_exporter sidecar or standalone to expose pg_* metrics",
            "Ensure pg_stat_statements extension is enabled for query-level visibility",
            "Configure WAL archiving for point-in-time recovery monitoring",
        ],
        recommendations=[
            "Use a StatefulSet with PVCs for data durability",
            "Set resource limits — PostgreSQL will use all available memory for shared_buffers",
            "Add a readiness probe on port 5432 (pg_isready)",
            "Monitor pg_stat_statements for slow query detection",
        ],
    )
)

# ── MySQL / MariaDB ───────────────────────────────────────────────────────────

_MYSQL_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_DATABASE,
        display_name="MySQL",
        description="Relational database — monitor connections, replication, InnoDB buffer pool, and slow queries.",
        exporter="mysqld_exporter",
        exporter_port=9104,
        golden_metrics=[
            MetricSignal(
                "mysql_connections",
                "mysql_global_status_threads_connected",
                "Current open connections",
                requires="exporter",
            ),
            MetricSignal(
                "mysql_queries_per_sec",
                "rate(mysql_global_status_queries[5m])",
                "Query throughput",
                requires="exporter",
            ),
            MetricSignal(
                "mysql_slow_queries",
                "rate(mysql_global_status_slow_queries[5m])",
                "Slow query rate",
                requires="exporter",
            ),
            MetricSignal(
                "mysql_innodb_buffer_pool_hit_ratio",
                "1 - (rate(mysql_global_status_innodb_buffer_pool_reads[5m]) / rate(mysql_global_status_innodb_buffer_pool_read_requests[5m]))",
                "InnoDB buffer pool hit ratio",
                panel_type="gauge",
                requires="exporter",
            ),
            MetricSignal(
                "mysql_replication_lag",
                "mysql_slave_status_seconds_behind_master",
                "Replication lag in seconds",
                requires="exporter,replicas>1",
            ),
        ],
        alerts=[
            AlertSignal(
                "MySQLTooManyConnections",
                "mysql_global_status_threads_connected > (mysql_global_variables_max_connections * 0.8)",
                severity="warning",
                for_duration="5m",
                summary="MySQL connection count exceeds 80% of max_connections",
                requires="exporter",
            ),
            AlertSignal(
                "MySQLReplicationLagHigh",
                "mysql_slave_status_seconds_behind_master > 30",
                severity="critical",
                for_duration="5m",
                summary="MySQL replication lag exceeds 30 seconds",
                requires="exporter,replicas>1",
            ),
            AlertSignal(
                "MySQLSlowQueryRateHigh",
                "rate(mysql_global_status_slow_queries[5m]) > 0.1",
                severity="warning",
                for_duration="10m",
                summary="Elevated slow query rate",
                requires="exporter",
            ),
        ],
        dashboard_tags=["mysql", "database"],
        health_requirements=[
            "Deploy mysqld_exporter sidecar to expose mysql_* metrics",
            "Enable performance_schema for query-level monitoring",
        ],
        recommendations=[
            "Use a StatefulSet with PVCs for data durability",
            "Set innodb_buffer_pool_size to ~70% of available memory",
            "Add a readiness probe using mysqladmin ping",
        ],
    )
)

# ── Redis ─────────────────────────────────────────────────────────────────────

_REDIS_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_CACHE,
        display_name="Redis",
        description="In-memory data store — monitor memory usage, evictions, hit rate, and connected clients.",
        exporter="redis_exporter",
        exporter_port=9121,
        golden_metrics=[
            MetricSignal(
                "redis_memory_used_bytes",
                "redis_memory_used_bytes",
                "Current memory usage",
                requires="exporter",
            ),
            MetricSignal(
                "redis_memory_max_bytes",
                "redis_memory_max_bytes",
                "Configured maxmemory limit",
                requires="exporter",
            ),
            MetricSignal(
                "redis_hit_rate",
                "rate(redis_keyspace_hits_total[5m]) / (rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m]))",
                "Cache hit ratio",
                panel_type="gauge",
                requires="exporter",
            ),
            MetricSignal(
                "redis_evicted_keys",
                "rate(redis_evicted_keys_total[5m])",
                "Key eviction rate — nonzero means memory pressure",
                requires="exporter",
            ),
            MetricSignal(
                "redis_connected_clients",
                "redis_connected_clients",
                "Current client connections",
                requires="exporter",
            ),
            MetricSignal(
                "redis_ops_per_sec",
                "rate(redis_commands_processed_total[5m])",
                "Command throughput",
                requires="exporter",
            ),
        ],
        alerts=[
            AlertSignal(
                "RedisMemoryNearMax",
                "redis_memory_used_bytes / redis_memory_max_bytes > 0.9",
                severity="warning",
                for_duration="5m",
                summary="Redis memory usage above 90% of maxmemory",
                requires="exporter",
            ),
            AlertSignal(
                "RedisEvictionsActive",
                "rate(redis_evicted_keys_total[5m]) > 0",
                severity="warning",
                for_duration="10m",
                summary="Redis is actively evicting keys — memory pressure",
                requires="exporter",
            ),
            AlertSignal(
                "RedisHighLatency",
                "redis_slowlog_length > 10",
                severity="warning",
                for_duration="5m",
                summary="Redis slowlog growing — possible performance degradation",
                requires="exporter",
            ),
        ],
        dashboard_tags=["redis", "cache"],
        health_requirements=[
            "Deploy redis_exporter sidecar to expose redis_* metrics",
            "Configure maxmemory and maxmemory-policy to prevent OOM kills",
        ],
        recommendations=[
            "Set maxmemory-policy (allkeys-lru for cache, noeviction for queues)",
            "Monitor keyspace hit ratio — below 90% indicates poor cache utilization",
            "Use a readiness probe via redis-cli ping",
        ],
    )
)

# ── MongoDB ───────────────────────────────────────────────────────────────────

_MONGO_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_DATABASE,
        display_name="MongoDB",
        description="Document database — monitor connections, oplog, replica set health, and WiredTiger cache.",
        exporter="mongodb_exporter",
        exporter_port=9216,
        golden_metrics=[
            MetricSignal("mongodb_connections_current", "mongodb_ss_connections{conn_type='current'}", "Current connections", requires="exporter"),
            MetricSignal("mongodb_opcounters", "rate(mongodb_ss_opcounters_total[5m])", "Operation counters (insert/query/update/delete)", requires="exporter"),
            MetricSignal("mongodb_repl_lag", "mongodb_mongod_replset_member_optime_date - mongodb_mongod_replset_member_optime_date{state='PRIMARY'}", "Replication lag", requires="exporter,replicas>1"),
            MetricSignal("mongodb_wiredtiger_cache", "mongodb_ss_wt_cache_bytes_currently_in_the_cache", "WiredTiger cache usage", requires="exporter"),
        ],
        alerts=[
            AlertSignal("MongoDBReplicationLag", "mongodb_mongod_replset_member_replication_lag > 10", severity="critical", for_duration="5m", summary="MongoDB replica set member lagging behind primary", requires="exporter,replicas>1"),
            AlertSignal("MongoDBConnectionsHigh", "mongodb_ss_connections{conn_type='current'} > 5000", severity="warning", for_duration="5m", summary="MongoDB connection count high", requires="exporter"),
        ],
        dashboard_tags=["mongodb", "database"],
        health_requirements=["Deploy mongodb_exporter to expose mongodb_* metrics"],
        recommendations=["Use a StatefulSet for replica set members", "Monitor oplog window size for replication health"],
    )
)

# ── Elasticsearch ─────────────────────────────────────────────────────────────

_ES_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_SEARCH_ENGINE,
        display_name="Elasticsearch",
        description="Search and analytics engine — monitor cluster health, JVM heap, indexing rate, and shard allocation.",
        exporter="elasticsearch_exporter",
        exporter_port=9114,
        golden_metrics=[
            MetricSignal("es_cluster_health", "elasticsearch_cluster_health_status", "Cluster health (green/yellow/red)", requires="exporter"),
            MetricSignal("es_jvm_heap_used", "elasticsearch_jvm_memory_used_bytes{area='heap'}", "JVM heap usage", requires="exporter"),
            MetricSignal("es_indexing_rate", "rate(elasticsearch_indices_indexing_index_total[5m])", "Document indexing rate", requires="exporter"),
            MetricSignal("es_search_latency", "elasticsearch_indices_search_fetch_time_seconds / elasticsearch_indices_search_fetch_total", "Average search latency", requires="exporter"),
            MetricSignal("es_unassigned_shards", "elasticsearch_cluster_health_unassigned_shards", "Unassigned shard count", requires="exporter"),
        ],
        alerts=[
            AlertSignal("ElasticsearchClusterRed", 'elasticsearch_cluster_health_status{color="red"} == 1', severity="critical", for_duration="1m", summary="Elasticsearch cluster health is RED", requires="exporter"),
            AlertSignal("ElasticsearchClusterYellow", 'elasticsearch_cluster_health_status{color="yellow"} == 1', severity="warning", for_duration="10m", summary="Elasticsearch cluster health is YELLOW", requires="exporter"),
            AlertSignal("ElasticsearchJVMHeapHigh", "elasticsearch_jvm_memory_used_bytes{area='heap'} / elasticsearch_jvm_memory_max_bytes{area='heap'} > 0.9", severity="warning", for_duration="5m", summary="Elasticsearch JVM heap usage above 90%", requires="exporter"),
        ],
        dashboard_tags=["elasticsearch", "search"],
        health_requirements=["Ensure /_cluster/health endpoint is accessible", "elasticsearch_exporter sidecar needed for prometheus metrics"],
        recommendations=["Set JVM heap to 50% of available memory (max 31 GB)", "Monitor unassigned shards — they indicate capacity or config issues"],
    )
)

# ── Kafka ─────────────────────────────────────────────────────────────────────

_KAFKA_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_MESSAGE_QUEUE,
        display_name="Kafka",
        description="Distributed event streaming — monitor consumer lag, under-replicated partitions, and broker throughput.",
        exporter="kafka_exporter / JMX exporter",
        exporter_port=9308,
        golden_metrics=[
            MetricSignal("kafka_consumer_lag", "kafka_consumergroup_lag", "Consumer group lag (messages behind)", requires="exporter"),
            MetricSignal("kafka_under_replicated_partitions", "kafka_server_replicamanager_underreplicatedpartitions", "Under-replicated partitions", requires="exporter"),
            MetricSignal("kafka_messages_in_per_sec", "rate(kafka_server_brokertopicmetrics_messagesin_total[5m])", "Message ingest rate", requires="exporter"),
            MetricSignal("kafka_isr_shrinks", "rate(kafka_server_replicamanager_isrshrinks_total[5m])", "ISR shrink rate — indicates broker instability", requires="exporter"),
        ],
        alerts=[
            AlertSignal("KafkaConsumerLagHigh", "kafka_consumergroup_lag > 10000", severity="warning", for_duration="10m", summary="Kafka consumer group lag exceeds 10k messages", requires="exporter"),
            AlertSignal("KafkaUnderReplicated", "kafka_server_replicamanager_underreplicatedpartitions > 0", severity="critical", for_duration="5m", summary="Kafka has under-replicated partitions — risk of data loss", requires="exporter,replicas>1"),
            AlertSignal("KafkaISRShrinking", "rate(kafka_server_replicamanager_isrshrinks_total[5m]) > 0", severity="warning", for_duration="5m", summary="Kafka ISR is shrinking — broker may be unhealthy", requires="exporter,replicas>1"),
        ],
        dashboard_tags=["kafka", "messaging"],
        health_requirements=["Deploy kafka_exporter or enable JMX exporter for kafka_* metrics", "Monitor ZooKeeper (or KRaft controller) health separately"],
        recommendations=["Set min.insync.replicas >= 2 for durability", "Monitor consumer lag per consumer group, not just globally"],
    )
)

# ── RabbitMQ ──────────────────────────────────────────────────────────────────

_RABBITMQ_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_MESSAGE_QUEUE,
        display_name="RabbitMQ",
        description="Message broker — monitor queue depth, consumer utilization, and node memory.",
        exporter="rabbitmq_prometheus (built-in)",
        exporter_port=15692,
        golden_metrics=[
            MetricSignal("rabbitmq_queue_messages", "rabbitmq_queue_messages", "Messages ready + unacknowledged per queue"),
            MetricSignal("rabbitmq_queue_consumers", "rabbitmq_queue_consumers", "Consumer count per queue"),
            MetricSignal("rabbitmq_node_mem_used", "rabbitmq_process_resident_memory_bytes", "Node resident memory"),
            MetricSignal("rabbitmq_publish_rate", "rate(rabbitmq_channel_messages_published_total[5m])", "Message publish rate"),
        ],
        alerts=[
            AlertSignal("RabbitMQQueueBacklog", "rabbitmq_queue_messages > 10000", severity="warning", for_duration="10m", summary="RabbitMQ queue depth exceeds 10k messages"),
            AlertSignal("RabbitMQNoConsumers", "rabbitmq_queue_consumers == 0 and rabbitmq_queue_messages > 0", severity="critical", for_duration="5m", summary="RabbitMQ queue has messages but no consumers"),
            AlertSignal("RabbitMQHighMemory", "rabbitmq_process_resident_memory_bytes / rabbitmq_node_mem_limit > 0.8", severity="warning", for_duration="5m", summary="RabbitMQ memory usage above 80% of limit"),
        ],
        dashboard_tags=["rabbitmq", "messaging"],
        health_requirements=["Enable the rabbitmq_prometheus plugin (ships with RabbitMQ 3.8+)"],
        recommendations=["Set per-queue message TTL and max-length policies", "Monitor individual queue depth, not just node-level aggregates"],
    )
)

# ── NATS ──────────────────────────────────────────────────────────────────────

_NATS_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_MESSAGE_QUEUE,
        display_name="NATS",
        description="Cloud-native messaging — monitor connection count, message throughput, and JetStream stream lag.",
        exporter="prometheus-nats-exporter",
        exporter_port=7777,
        golden_metrics=[
            MetricSignal("nats_connections", "nats_varz_connections", "Active client connections", requires="exporter"),
            MetricSignal("nats_messages_in", "rate(nats_varz_in_msgs[5m])", "Inbound message rate", requires="exporter"),
            MetricSignal("nats_slow_consumers", "nats_varz_slow_consumers", "Slow consumer count", requires="exporter"),
        ],
        alerts=[
            AlertSignal("NATSSlowConsumers", "nats_varz_slow_consumers > 0", severity="warning", for_duration="5m", summary="NATS has slow consumers — messages may be dropped", requires="exporter"),
        ],
        dashboard_tags=["nats", "messaging"],
        health_requirements=["Deploy prometheus-nats-exporter sidecar"],
        recommendations=["Monitor JetStream consumer ack-pending for delivery guarantees"],
    )
)

# ── nginx ─────────────────────────────────────────────────────────────────────

_NGINX_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_WEB_SERVER,
        display_name="NGINX",
        description="Web server / reverse proxy — monitor active connections, request rate, upstream response times, and error rates.",
        exporter="nginx-prometheus-exporter (stub_status) or nginx-vts-exporter",
        exporter_port=9113,
        golden_metrics=[
            MetricSignal("nginx_active_connections", "nginx_connections_active", "Currently active client connections", requires="exporter"),
            MetricSignal("nginx_request_rate", "rate(nginx_http_requests_total[5m])", "HTTP request throughput", requires="exporter"),
            MetricSignal("nginx_5xx_rate", 'rate(nginx_http_requests_total{status=~"5.."}[5m])', "5xx error rate", requires="exporter"),
            MetricSignal("nginx_upstream_response_time", 'nginx_upstream_response_time_seconds{quantile="0.95"}', "95th percentile upstream response time", requires="exporter"),
        ],
        alerts=[
            AlertSignal("NginxHighErrorRate", 'rate(nginx_http_requests_total{status=~"5.."}[5m]) / rate(nginx_http_requests_total[5m]) > 0.05', severity="critical", for_duration="5m", summary="NGINX 5xx error rate exceeds 5%", requires="exporter"),
            AlertSignal("NginxConnectionsNearLimit", "nginx_connections_active > 900", severity="warning", for_duration="5m", summary="NGINX active connections approaching worker_connections limit", requires="exporter"),
        ],
        dashboard_tags=["nginx", "web"],
        health_requirements=["Enable stub_status or the VTS module for metrics exposure", "Deploy nginx-prometheus-exporter sidecar"],
        recommendations=["Add upstream health checks in nginx.conf", "Set worker_connections based on expected concurrent load"],
    )
)

# ── Envoy / Istio sidecar ────────────────────────────────────────────────────

_ENVOY_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_REVERSE_PROXY,
        display_name="Envoy",
        description="Service proxy — monitor request latency percentiles, circuit breaker state, and upstream health.",
        exporter="built-in (/stats/prometheus)",
        exporter_port=9901,
        golden_metrics=[
            MetricSignal("envoy_request_rate", "rate(envoy_http_downstream_rq_total[5m])", "Downstream request rate"),
            MetricSignal("envoy_5xx_rate", 'rate(envoy_http_downstream_rq_xx{envoy_response_code_class="5"}[5m])', "5xx response rate"),
            MetricSignal("envoy_p99_latency", 'histogram_quantile(0.99, rate(envoy_http_downstream_rq_time_bucket[5m]))', "p99 request latency"),
            MetricSignal("envoy_cx_active", "envoy_http_downstream_cx_active", "Active downstream connections"),
        ],
        alerts=[
            AlertSignal("EnvoyHighLatency", 'histogram_quantile(0.99, rate(envoy_http_downstream_rq_time_bucket[5m])) > 1', severity="warning", for_duration="5m", summary="Envoy p99 latency exceeds 1 second"),
            AlertSignal("EnvoyCircuitBreakerTripped", "envoy_cluster_circuit_breakers_default_cx_open > 0", severity="critical", for_duration="1m", summary="Envoy circuit breaker is open — upstream is unhealthy"),
        ],
        dashboard_tags=["envoy", "proxy", "service-mesh"],
        health_requirements=["Ensure /stats/prometheus endpoint is not blocked by network policy"],
        recommendations=["Configure circuit breakers per upstream cluster", "Monitor retry budgets to avoid retry storms"],
    )
)

# ── HAProxy ───────────────────────────────────────────────────────────────────

_HAPROXY_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_REVERSE_PROXY,
        display_name="HAProxy",
        description="Load balancer — monitor backend health, session rate, and queue depth.",
        exporter="haproxy_exporter or built-in prometheus endpoint",
        exporter_port=8405,
        golden_metrics=[
            MetricSignal("haproxy_backend_up", "haproxy_backend_up", "Backend server health"),
            MetricSignal("haproxy_session_rate", "rate(haproxy_frontend_sessions_total[5m])", "Frontend session rate"),
            MetricSignal("haproxy_queue_current", "haproxy_backend_current_queue", "Backend queue depth"),
        ],
        alerts=[
            AlertSignal("HAProxyBackendDown", "haproxy_backend_up == 0", severity="critical", for_duration="1m", summary="HAProxy backend is completely down"),
            AlertSignal("HAProxyQueueBacklog", "haproxy_backend_current_queue > 100", severity="warning", for_duration="5m", summary="HAProxy backend queue building up"),
        ],
        dashboard_tags=["haproxy", "loadbalancer"],
        health_requirements=["Enable the Prometheus endpoint in haproxy.cfg"],
        recommendations=["Monitor per-backend server health individually"],
    )
)

# ── Prometheus ────────────────────────────────────────────────────────────────

_PROM_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_MONITORING,
        display_name="Prometheus",
        description="Monitoring system — monitor scrape health, TSDB size, rule evaluation duration, and WAL corruption.",
        exporter="built-in (/metrics)",
        exporter_port=9090,
        golden_metrics=[
            MetricSignal("prometheus_tsdb_head_series", "prometheus_tsdb_head_series", "Active time series count"),
            MetricSignal("prometheus_target_scrape_failures", "rate(prometheus_target_scrapes_failed_total[5m])", "Scrape failure rate"),
            MetricSignal("prometheus_rule_eval_duration", "prometheus_rule_evaluation_duration_seconds", "Rule evaluation latency"),
        ],
        alerts=[
            AlertSignal("PrometheusTargetDown", "up == 0", severity="critical", for_duration="5m", summary="Prometheus scrape target is down"),
            AlertSignal("PrometheusTSDBCompactionFailing", "rate(prometheus_tsdb_compactions_failed_total[5m]) > 0", severity="warning", for_duration="15m", summary="Prometheus TSDB compaction failures"),
        ],
        dashboard_tags=["prometheus", "monitoring"],
        health_requirements=["Prometheus exposes its own /metrics endpoint by default"],
        recommendations=["Monitor cardinality — high series counts cause memory issues", "Set --storage.tsdb.retention.size to prevent disk exhaustion"],
    )
)

# ── Grafana ───────────────────────────────────────────────────────────────────

_GRAFANA_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_MONITORING,
        display_name="Grafana",
        description="Visualization platform — monitor datasource health and API latency.",
        exporter="built-in (/metrics)",
        exporter_port=3000,
        golden_metrics=[
            MetricSignal("grafana_http_request_duration", 'histogram_quantile(0.95, rate(grafana_http_request_duration_seconds_bucket[5m]))', "p95 API latency"),
            MetricSignal("grafana_datasource_errors", "rate(grafana_datasource_request_total{status='error'}[5m])", "Datasource error rate"),
        ],
        alerts=[
            AlertSignal("GrafanaDatasourceErrors", "rate(grafana_datasource_request_total{status='error'}[5m]) > 0.5", severity="warning", for_duration="5m", summary="Grafana datasource errors elevated"),
        ],
        dashboard_tags=["grafana", "monitoring"],
        health_requirements=["Enable built-in Prometheus metrics in grafana.ini"],
        recommendations=["Monitor dashboard load times for user experience"],
    )
)

# ── Fluentd / Fluentbit ──────────────────────────────────────────────────────

_FLUENTD_PROFILE = _register(
    ArchetypeProfile(
        archetype=ARCHETYPE_LOGGING,
        display_name="Fluentd/Fluent Bit",
        description="Log collector — monitor buffer queue length, retry rate, and output errors.",
        exporter="built-in (in_prometheus plugin)",
        exporter_port=24231,
        golden_metrics=[
            MetricSignal("fluentd_buffer_queue_length", "fluentd_output_status_buffer_queue_length", "Buffer queue depth"),
            MetricSignal("fluentd_retry_count", "rate(fluentd_output_status_retry_count[5m])", "Output retry rate"),
            MetricSignal("fluentd_emit_records", "rate(fluentd_output_status_emit_records[5m])", "Record emission rate"),
        ],
        alerts=[
            AlertSignal("FluentdBufferFull", "fluentd_output_status_buffer_queue_length > 256", severity="critical", for_duration="5m", summary="Fluentd buffer queue is full — logs may be dropped"),
            AlertSignal("FluentdRetryHigh", "rate(fluentd_output_status_retry_count[5m]) > 1", severity="warning", for_duration="10m", summary="Fluentd retry rate elevated — output destination may be unhealthy"),
        ],
        dashboard_tags=["fluentd", "logging"],
        health_requirements=["Enable the in_prometheus plugin for fluentd_* metrics"],
        recommendations=["Size buffers based on peak log throughput", "Monitor retry count per output plugin"],
    )
)


# ──────────────────────────── Exporter Detection ──────────────────────────────
# Maps exporter name substrings to image regex patterns used by the
# capability-inference layer in scanner.py.  If any container in a pod
# matches one of these, we know the corresponding domain metrics are
# available without needing cluster access.

EXPORTER_IMAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "postgres_exporter":      re.compile(r"postgres[_-]?exporter", re.I),
    "mysqld_exporter":        re.compile(r"mysql[d]?[_-]?exporter", re.I),
    "redis_exporter":         re.compile(r"redis[_-]?exporter", re.I),
    "mongodb_exporter":       re.compile(r"mongo(db)?[_-]?exporter", re.I),
    "elasticsearch_exporter": re.compile(r"elasticsearch[_-]?exporter", re.I),
    "kafka_exporter":         re.compile(r"kafka[_-]?exporter|jmx[_-]?exporter", re.I),
    "nats_exporter":          re.compile(r"(prometheus[_-])?nats[_-]?exporter", re.I),
    "nginx_exporter":         re.compile(r"nginx[_-]?(prometheus[_-])?exporter|nginx[_-]vts", re.I),
    "haproxy_exporter":       re.compile(r"haproxy[_-]?exporter", re.I),
    "node_exporter":          re.compile(r"node[_-]?exporter", re.I),
}

# Profiles whose metrics are exposed by the main container itself (no sidecar needed).
BUILTIN_METRICS_PROFILES: set[str] = {
    "rabbitmq", "envoy", "haproxy", "prometheus", "grafana",
    "fluentd_fluent_bit", "fluentd",
}


# ──────────────────────────── Image → Profile Mapping ─────────────────────────
# This is the core matching logic. Order matters — first match wins.
# Each entry is (regex_pattern, archetype_key, profile).

@dataclass
class _ImageRule:
    """Maps a container image regex to an ArchetypeProfile."""

    pattern: re.Pattern[str]
    profile: ArchetypeProfile


_IMAGE_RULES: list[_ImageRule] = [
    # Databases — each rule maps to the correct technology-specific profile
    _ImageRule(re.compile(r"(^|/)postgres(ql)?[:\-]|/pg[_-]", re.I), _PG_PROFILE),
    _ImageRule(re.compile(r"(^|/)(mysql|mariadb|percona)[:\-]", re.I), _MYSQL_PROFILE),
    _ImageRule(re.compile(r"(^|/)mongo(db)?[:\-]", re.I), _MONGO_PROFILE),
    # Cache
    _ImageRule(re.compile(r"(^|/)(redis|valkey|keydb|dragonfly)[:\-]", re.I), _REDIS_PROFILE),
    _ImageRule(re.compile(r"(^|/)memcache(d)?[:\-]", re.I), _REDIS_PROFILE),  # close enough archetype
    # Search
    _ImageRule(re.compile(r"(^|/)(elasticsearch|opensearch)[:\-]", re.I), _ES_PROFILE),
    _ImageRule(re.compile(r"(^|/)elastic/elasticsearch", re.I), _ES_PROFILE),
    # Message queues — each maps to the correct broker profile
    _ImageRule(re.compile(r"(^|/)(kafka|confluentinc/cp-kafka|bitnami/kafka)[:\-]", re.I), _KAFKA_PROFILE),
    _ImageRule(re.compile(r"(^|/)rabbitmq[:\-]", re.I), _RABBITMQ_PROFILE),
    _ImageRule(re.compile(r"(^|/)nats[:\-]", re.I), _NATS_PROFILE),
    # Web servers
    _ImageRule(re.compile(r"(^|/)nginx[:\-]", re.I), _NGINX_PROFILE),
    _ImageRule(re.compile(r"(^|/)(httpd|apache)[:\-]", re.I), _NGINX_PROFILE),  # similar archetype
    _ImageRule(re.compile(r"(^|/)caddy[:\-]", re.I), _NGINX_PROFILE),
    # Proxies / mesh
    _ImageRule(re.compile(r"(^|/)envoy(proxy)?[:\-]", re.I), _ENVOY_PROFILE),
    _ImageRule(re.compile(r"(^|/)haproxy[:\-]", re.I), _HAPROXY_PROFILE),
    _ImageRule(re.compile(r"(^|/)istio/proxyv2", re.I), _ENVOY_PROFILE),
    _ImageRule(re.compile(r"(^|/)traefik[:\-]", re.I), _ENVOY_PROFILE),  # closest archetype
    # Monitoring
    _ImageRule(re.compile(r"(^|/)prom(etheus)?/prometheus", re.I), _PROM_PROFILE),
    _ImageRule(re.compile(r"(^|/)grafana/grafana", re.I), _GRAFANA_PROFILE),
    # Logging
    _ImageRule(re.compile(r"(^|/)(fluentd|fluent-bit|fluent/fluent-bit)[:\-]", re.I), _FLUENTD_PROFILE),
]


# ──────────────────────────── Port / env / label heuristics ───────────────────

_PORT_HINTS: dict[int, tuple[str, str]] = {
    # (archetype, profile_key)
    5432: ("database", "postgresql"),
    3306: ("database", "mysql"),
    27017: ("database", "mongodb"),
    6379: ("cache", "redis"),
    11211: ("cache", "redis"),         # Memcached — closest profile
    9200: ("search-engine", "elasticsearch"),
    9092: ("message-queue", "kafka"),
    5672: ("message-queue", "rabbitmq"),
    4222: ("message-queue", "nats"),
    9090: ("monitoring", "prometheus"),
    3000: ("monitoring", "grafana"),
}

_ENV_HINTS: dict[str, tuple[str, str]] = {
    # (archetype, profile_key)
    "POSTGRES_PASSWORD": ("database", "postgresql"),
    "POSTGRES_DB": ("database", "postgresql"),
    "PGDATA": ("database", "postgresql"),
    "MYSQL_ROOT_PASSWORD": ("database", "mysql"),
    "MYSQL_DATABASE": ("database", "mysql"),
    "MONGO_INITDB_ROOT_USERNAME": ("database", "mongodb"),
    "REDIS_PASSWORD": ("cache", "redis"),
    "REDIS_URL": ("cache", "redis"),
    "ELASTICSEARCH_HOSTS": ("search-engine", "elasticsearch"),
    "KAFKA_BROKER_ID": ("message-queue", "kafka"),
    "KAFKA_ZOOKEEPER_CONNECT": ("message-queue", "kafka"),
    "RABBITMQ_DEFAULT_USER": ("message-queue", "rabbitmq"),
}


@dataclass
class Classification:
    """Result of classifying a container image.

    ``confidence`` is a qualitative bucket (``"high"``/``"medium"``/``"low"``)
    for backward compatibility.  ``score`` is a numeric value in 0.0–1.0 that
    captures the aggregate evidence strength — the LLM can use this to decide
    whether to emit technology-specific alerts or fall back to generic ones.
    """

    archetype: str
    profile: ArchetypeProfile | None
    confidence: str  # "high" (image match), "medium" (port/env), "low" (fallback)
    score: float  # 0.0–1.0  numeric confidence
    match_source: str  # what triggered the match: "image", "port", "env", "label", "fallback"
    evidence: list[str] = field(default_factory=list)  # human-readable evidence trail


# ──────────────────────────── Evidence Weights ────────────────────────────────
# These are calibrated so that:
#   image alone      → 0.70 ("high")
#   image + port     → 0.95
#   image + port + env → 1.00 (capped)
#   port alone       → 0.25 ("medium")
#   env alone        → 0.15 ("medium")
#   label alone      → 0.20 ("medium")
#   port + env       → 0.40 ("medium")
#   fallback         → 0.10 ("low")
#
# Threshold mapping:  ≥0.60 → high  |  ≥0.15 → medium  |  <0.15 → low
# Multiple signals for the *same* profile accumulate (capped at 1.0).
# Conflicting signals (different profiles) don't combine — highest total wins.

_W_IMAGE = 0.70
_W_PORT = 0.25
_W_ENV = 0.15
_W_LABEL = 0.20


def _bucket(score: float) -> str:
    """Map a numeric score to a qualitative confidence bucket."""
    if score >= 0.60:
        return "high"
    if score >= 0.15:
        return "medium"
    return "low"


def classify_image(
    image: str,
    ports: list[int] | None = None,
    env_vars: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> Classification:
    """Classify a container image into a workload archetype.

    Rather than first-match-wins, this function accumulates evidence from every
    available signal source and picks the profile with the highest aggregate
    score.  The numeric ``score`` (0.0–1.0) is attached to the result so
    downstream consumers (the LLM, the renderer) can modulate their output
    depending on detection certainty.

    Evidence sources (in weight order):
    1. Image name regex match              – weight 0.70
    2. Exposed container ports             – weight 0.25
    3. Environment variable names          – weight 0.15
    4. Kubernetes labels                   – weight 0.20
    """
    # Accumulator: profile_key → (score, profile, list[evidence_string])
    candidates: dict[str, tuple[float, ArchetypeProfile, list[str]]] = {}

    def _add(key: str, profile: ArchetypeProfile, weight: float, reason: str) -> None:
        prev_score, _, prev_ev = candidates.get(key, (0.0, profile, []))
        candidates[key] = (prev_score + weight, profile, [*prev_ev, reason])

    # 1. Image regex
    for rule in _IMAGE_RULES:
        if rule.pattern.search(image):
            rk = _registry_key(rule.profile)
            _add(rk, rule.profile, _W_IMAGE, f"image:{image}")
            break  # only the first matching image rule fires

    # 2. Port heuristics
    for port in (ports or []):
        if port in _PORT_HINTS:
            archetype, profile_key = _PORT_HINTS[port]
            profile = _PROFILES.get(profile_key)
            if profile:
                _add(profile_key, profile, _W_PORT, f"port:{port}")
            else:
                # Profile not registered — still record archetype evidence
                _add(
                    f"_unresolved_{archetype}",
                    ArchetypeProfile(archetype=archetype, display_name=archetype, description=""),
                    _W_PORT,
                    f"port:{port}",
                )

    # 3. Environment variable heuristics
    seen_env_profiles: set[str] = set()
    for env in (env_vars or []):
        if env in _ENV_HINTS:
            archetype, profile_key = _ENV_HINTS[env]
            if profile_key in seen_env_profiles:
                continue  # don't double-count multiple env vars for the same profile
            seen_env_profiles.add(profile_key)
            profile = _PROFILES.get(profile_key)
            if profile:
                _add(profile_key, profile, _W_ENV, f"env:{env}")

    # 4. Label heuristics (app.kubernetes.io/name)
    if labels:
        app_name = labels.get("app.kubernetes.io/name", "").lower()
        if app_name:
            probe = app_name if ":" in app_name or "-" in app_name else app_name + ":"
            for rule in _IMAGE_RULES:
                if rule.pattern.search(probe):
                    rk = _registry_key(rule.profile)
                    _add(rk, rule.profile, _W_LABEL, f"label:app.kubernetes.io/name={app_name}")
                    break

    # Pick the candidate with the highest accumulated score
    if candidates:
        best_key = max(candidates, key=lambda k: candidates[k][0])
        raw_score, best_profile, evidence = candidates[best_key]
        score = min(raw_score, 1.0)
        # Determine primary match source (the highest-weight evidence)
        primary = evidence[0] if evidence else "unknown"
        if primary.startswith("image:"):
            source = "image"
        elif primary.startswith("port:"):
            source = primary  # e.g. "port:5432"
        elif primary.startswith("env:"):
            source = primary  # e.g. "env:POSTGRES_PASSWORD"
        elif primary.startswith("label:"):
            source = primary
        else:
            source = primary

        return Classification(
            archetype=best_profile.archetype,
            profile=best_profile,
            confidence=_bucket(score),
            score=round(score, 2),
            match_source=source,
            evidence=evidence,
        )

    # 5. Fallback — no evidence at all
    return Classification(
        archetype=ARCHETYPE_CUSTOM_APP,
        profile=None,
        confidence="low",
        score=0.10,
        match_source="fallback",
        evidence=["no matching signals"],
    )


def _registry_key(profile: ArchetypeProfile) -> str:
    """Derive the canonical registry key for a profile instance."""
    return profile.key or profile.display_name.lower().replace(" ", "_").replace("/", "_")


def get_profile(archetype: str) -> ArchetypeProfile | None:
    """Look up an archetype profile by name."""
    return _PROFILES.get(archetype)


def all_profiles() -> dict[str, ArchetypeProfile]:
    """Return all registered archetype profiles."""
    return dict(_PROFILES)
