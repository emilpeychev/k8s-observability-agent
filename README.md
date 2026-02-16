# k8s-observability-agent

AI-powered agent that replaces an observability engineer. It scans Git repositories to detect infrastructure, proposes battle-tested dashboards, validates everything on a live cluster, and fixes what's broken.

Unlike generic "add CPU/memory alerts" tools, this agent **fingerprints your workloads** — it knows that a PostgreSQL StatefulSet needs replication-lag alerts and WAL archive panels, while a Kafka cluster needs consumer-lag tracking and under-replicated partition alerts. The difference between generic and intelligent observability output comes from the classification layer, not the LLM.

## What it does

### 1. Detect infrastructure from code

- **Scans** a Git repository for Kubernetes manifests (Deployments, Services, StatefulSets, Ingresses, ConfigMaps, etc.)
- **Classifies** every container image into a technology profile using evidence accumulation — image regex, ports, environment variables, and Kubernetes labels all contribute a weighted confidence score
- **Analyses** the platform — discovers resource relationships (Service→Deployment, Ingress→Service, HPA→target), identifies health/monitoring gaps including missing exporters

### 2. Propose dashboards and alerting

- **Recommends ready-made community Grafana dashboards** from grafana.com for each detected workload type — no need to build panels from scratch
- **Generates** technology-specific Prometheus alerting rules with **nodata calibration** — each alert has an appropriate `nodata_state` (`ok`, `alerting`, or `nodata`) based on how critical the metric is
- **Produces** a human-readable Markdown summary with best-practice recommendations

### 3. Test on a live cluster

- **Connects** to a Kubernetes cluster via kubectl
- **Auto-discovers** Prometheus and Grafana instances in the cluster
- **Validates** scrape targets are healthy, metrics exist, alert expressions evaluate correctly
- **Imports** recommended community dashboards into Grafana
- **Generates** a validation report with pass/fail/warn checks

### 4. Fix what's broken

- **Deploys** missing exporters and ServiceMonitors (with `--allow-writes`)
- **Applies** Prometheus rules and Grafana datasource configuration
- **Diagnoses** issues using pod logs, events, and cluster state
- **Reports** exactly what was fixed and what still needs attention

### 5. HTML report served in-cluster

- **Renders** a styled HTML validation report from the agent's findings using Jinja2
- **Deploys** the report as an nginx pod in the `observability-report` namespace
- **Exposes** it via Istio Gateway + VirtualService at `http://report.local`
- **Adds** `report.local` → Istio gateway IP to `/etc/hosts` automatically
- Report includes: validation checks, imported dashboards, suggested community dashboards, alert rules, metrics, and action items

## Workload Classification

The classifier is the core intelligence layer. It runs **before** the LLM, so the agent already knows what each workload *is* before it starts reasoning.

### Two-layer ontology

```
Container Image
   ↓
Archetype     (what role it plays: database, cache, message-queue, …)
   ↓
Profile       (what technology it is: PostgreSQL, Redis, Kafka, …)
   ↓
Monitoring Strategy  (golden metrics, alerts, exporter, dashboards)
```

### Supported profiles

| Archetype | Profiles | Exporter |
|-----------|----------|----------|
| database | PostgreSQL, MySQL, MongoDB | postgres_exporter, mysqld_exporter, mongodb_exporter |
| cache | Redis | redis_exporter |
| search-engine | Elasticsearch | elasticsearch_exporter |
| message-queue | Kafka, RabbitMQ, NATS | kafka_exporter, built-in, prometheus-nats-exporter |
| web-server | NGINX | nginx-prometheus-exporter |
| reverse-proxy | Envoy, HAProxy | built-in, haproxy_exporter |
| monitoring | Prometheus, Grafana | built-in |
| logging | Fluentd/Fluent Bit | built-in |

### Evidence accumulation

Classification is **not** first-match-wins. Every signal source contributes a weighted score to a per-profile accumulator, and the highest total wins:

| Signal | Weight | Example |
|--------|--------|---------|
| Image regex | 0.70 | `postgres:15` matches PostgreSQL profile |
| Container port | 0.25 | port 5432 → PostgreSQL |
| Env variable | 0.15 | `POSTGRES_PASSWORD` → PostgreSQL |
| K8s label | 0.20 | `app.kubernetes.io/name: postgresql` |

The numeric score (0.0–1.0) is attached to every container and passed to the LLM:

- **≥ 0.60 (high)** — emit technology-specific alerts and golden metrics
- **0.15–0.59 (medium)** — use archetype-level alerts, flag detection as uncertain
- **< 0.15 (low)** — fall back to generic Kubernetes metrics, recommend manual verification

```
PostgreSQL detected (score: 0.95)
  + image:postgres:15
  + port:5432
  + env:POSTGRES_DB
→ emit pg_replication_lag_bytes, pg_stat_activity_count, WAL panels
```

## Installation

```bash
pip install -e .
```

Or with dev dependencies:

```bash
pip install -e ".[dev]"
```

## Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- For live cluster validation: `kubectl` configured and pointing at a cluster
- For HTTPS endpoints with custom CA: the CA certificate file (e.g. `tls/ca.crt` from your cluster setup)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

### Full analysis (with AI)

```bash
# Analyse a local repository
k8s-obs analyze /path/to/k8s-repo

# Analyse a GitHub repository
k8s-obs analyze --github https://github.com/org/repo --branch main

# Customise output
k8s-obs analyze /path/to/repo -o ./my-output --model claude-sonnet-4-20250514 -v
```

### Scan only (no AI, just platform summary)

```bash
k8s-obs scan /path/to/k8s-repo
k8s-obs scan --github https://github.com/org/repo
```

### Validate on a live cluster

```bash
# Auto-discover Prometheus & Grafana in the cluster
k8s-obs validate

# With a local CA certificate (e.g. Kind cluster with custom CA)
k8s-obs validate \
  --prometheus-url https://prometheus.local \
  --grafana-url https://grafana.local \
  --ca-cert /path/to/tls/ca.crt

# Fallback: port-forward when no CA cert is available
kubectl port-forward -n monitoring svc/prometheus-server 9090:80 &
kubectl port-forward -n monitoring svc/grafana 3000:80 &
k8s-obs validate \
  --prometheus-url http://localhost:9090 \
  --grafana-url http://localhost:3000

# Validate a previously generated plan
k8s-obs validate --plan observability-output/plan.json

# Allow the agent to apply fixes (deploy exporters, import dashboards, etc.)
k8s-obs validate --allow-writes

# Custom Grafana password (default is admin/admin)
k8s-obs validate --grafana-password myNewPassword

# Full pipeline: scan repo, then validate on cluster
k8s-obs analyze ./my-repo -o output/
k8s-obs validate --plan output/plan.json --allow-writes
```

### CLI reference

```
k8s-obs analyze --help
k8s-obs scan --help
k8s-obs validate --help
```

| Command | Purpose | Requires API key | Requires cluster |
|---------|---------|:---:|:---:|
| `analyze` | Scan repo + AI analysis → generate monitoring configs | Yes | No |
| `scan` | Scan repo → print platform summary | No | No |
| `validate` | Connect to cluster → test observability → fix issues | Yes | Yes |

### Validate CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--prometheus-url` | *(auto-discover)* | Prometheus URL. Skips discovery when set |
| `--grafana-url` | *(auto-discover)* | Grafana URL. Skips discovery when set |
| `--ca-cert` | *(none)* | Path to CA certificate for TLS verification (e.g. `tls/ca.crt`) |
| `--grafana-api-key` | *(none)* | Grafana API key or service-account token |
| `--grafana-password` | `admin` | Grafana admin password for basic auth |
| `--allow-writes` | `false` | Let the agent apply manifests to the cluster |
| `--plan` | *(none)* | Path to a previously generated `plan.json` to validate against |
| `--kubeconfig` | `~/.kube/config` | Path to kubeconfig file |
| `--context` | *(current)* | Kubernetes context to use |
| `--model` | `claude-sonnet-4-20250514` | Anthropic model |
| `--max-turns` | `40` | Maximum agent reasoning turns |
| `-o` / `--output` | `observability-output` | Output directory |
| `-v` / `--verbose` | `false` | Show full agent reasoning |

## Output

The agent writes to the output directory (default: `observability-output/`):

| File | Description |
|------|-------------|
| `prometheus-rules.yml` | Prometheus alerting rules — technology-specific PromQL with nodata calibration |
| `grafana-*.json` | Grafana dashboard JSON definitions grouped by archetype |
| `observability-plan.md` | Human-readable summary with dashboard recommendations and action items |
| `validation_report.json` | *(validate only)* Structured validation report with pass/fail checks |
| `report.html` | *(validate only)* Styled HTML report — also deployed as a pod at `http://report.local` |

## Architecture

```
k8s_observability_agent/
  cli.py          # Click CLI — analyze, scan, validate commands
  config.py       # Settings & configuration (repo + cluster)
  models.py       # Pydantic models for K8s resources, plans, & validation reports
  scanner.py      # Git repo scanning & K8s manifest parsing
  classifier.py   # Workload fingerprinting (archetype → profile → metrics)
  analyzer.py     # Platform relationship analysis & gap detection
  core.py         # Claude agent loops (analyze + validate)
  renderer.py     # Jinja2 template rendering for outputs
  cluster.py      # Kubernetes cluster interaction via kubectl
  prometheus.py   # Prometheus HTTP API client
  grafana.py      # Grafana HTTP API client
  tools/
    registry.py   # Repo-analysis tool definitions for the agent
    live.py       # Live-cluster tool definitions for the validation agent
  templates/
    prometheus_rules.yml.j2
    grafana_dashboard.json.j2
    plan_summary.md.j2
    validation_report.html.j2
tests/
  test_models.py        # Model unit tests
  test_scanner.py       # Scanner tests
  test_classifier.py    # Classifier tests (knowledge system)
  test_analyzer.py      # Analyzer tests
  test_tools.py         # Repo tool registry tests
  test_renderer.py      # Renderer tests
  test_cluster.py       # Cluster client tests
  test_prometheus.py    # Prometheus client tests
  test_grafana.py       # Grafana client tests
  test_live_tools.py    # Live-cluster tool tests
```

### Pipeline

```
                 ANALYZE (offline)                        VALIDATE (live cluster)
┌────────────────────────────────────────┐   ┌──────────────────────────────────────┐
│                                        │   │                                      │
│ Git Repo → Scanner → Classifier →      │   │ kubectl → Discover Prometheus/Grafana│
│            Analyzer → LLM Agent →      │   │        → Check scrape targets        │
│            Renderer → Output files     │   │        → Validate metrics & alerts    │
│                                        │   │        → Import dashboards            │
│ Tools: list_resources, get_insights,   │   │        → Deploy fixes (if --allow)    │
│        check_gaps, generate_plan       │   │        → Validation report            │
│                                        │   │                                      │
│ Output: prometheus-rules.yml           │   │ Tools: check_connectivity, find_stack,│
│         grafana-dashboard-*.json       │   │        check_scrape_targets,          │
│         observability-plan.md          │   │        validate_metric_exists,        │
│                                        │   │        import_grafana_dashboard,      │
│                                        │   │        apply_kubernetes_manifest, ... │
└────────────────────────────────────────┘   └──────────────────────────────────────┘
```

## How the agent works

The agent uses Claude's tool-calling capability in two agentic loops:

### Analyze mode (offline)

1. The platform summary is sent as context to Claude
2. Claude calls tools to explore resources, check gaps, and retrieve **archetype-specific observability knowledge**
3. After thorough analysis, Claude calls `generate_observability_plan` with structured data
4. The structured plan is rendered into Prometheus rules, Grafana dashboards, and a Markdown report

| Tool | Purpose |
|------|---------|
| `list_resources` | List/filter discovered K8s resources |
| `get_resource_detail` | Inspect a specific resource (containers, probes, limits, labels) |
| `get_relationships` | View Service→Deployment, Ingress→Service, HPA→target mappings |
| `get_platform_summary` | High-level platform overview with resource counts |
| `check_health_gaps` | Missing probes, resource limits, orphaned selectors, **missing exporters** |
| `get_workload_insights` | **Key tool** — returns golden metrics, alert rules, exporter requirements, community dashboard IDs, and nodata calibration per classified workload |
| `generate_observability_plan` | Submit the final structured plan |

### Validate mode (live cluster)

1. The agent connects to the cluster and discovers Prometheus/Grafana
2. It validates scrape targets, checks metric existence, tests alert expressions
3. It imports recommended community dashboards into Grafana
4. With `--allow-writes`, it deploys missing exporters and ServiceMonitors
5. It generates a structured validation report with pass/fail/warn checks

| Tool | Purpose |
|------|---------|
| `check_cluster_connectivity` | Verify cluster is reachable |
| `find_monitoring_stack` | Auto-discover Prometheus and Grafana services |
| `get_cluster_resources` | List live K8s resources by kind/namespace/label |
| `describe_cluster_resource` | Show resource status, events, conditions |
| `get_pod_logs` | Fetch recent logs for diagnosing issues |
| `get_cluster_events` | Spot CrashLoopBackOff, pull errors, etc. |
| `check_scrape_targets` | Prometheus target health per job |
| `validate_metric_exists` | Batch check whether metrics exist in Prometheus |
| `run_promql_query` | Execute arbitrary PromQL to test alert expressions |
| `get_prometheus_alerts` | Currently firing/pending alerts |
| `get_prometheus_rules` | Configured alerting/recording rules |
| `list_grafana_dashboards` | Dashboards installed in Grafana |
| `check_grafana_datasources` | Verify Prometheus datasource is configured |
| `import_grafana_dashboard` | Import a community dashboard by grafana.com ID |
| `apply_kubernetes_manifest` | Deploy manifests (exporters, ServiceMonitors) — requires `--allow-writes` |
| `generate_validation_report` | Submit the final validation report |

## Development

```bash
pip install -e ".[dev]"
pytest                     # 196 tests
ruff check . && ruff format --check .
```

## Step-by-step guide

### 1. Install

```bash
git clone https://github.com/emilpeychev/k8s-observability-agent.git
cd k8s-observability-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

You can also pass it inline with `--api-key`.

### 3. Point the agent at a repository

**Local repo** — if your Kubernetes manifests live on disk:

```bash
k8s-obs analyze /path/to/my-k8s-repo
```

**GitHub repo** — the agent will shallow-clone it for you:

```bash
k8s-obs analyze --github https://github.com/org/infra --branch main
```

### 4. Read the output

After the agent finishes, check the `observability-output/` directory (or the path you set with `-o`):

```
observability-output/
  prometheus-rules.yml      # Drop into Prometheus / Thanos / Mimir
  grafana-dashboard-*.json  # Import into Grafana
  observability-plan.md     # Human-readable summary & recommendations
```

### 5. Scan only (no AI, no API key needed)

If you just want the platform analysis — workload classification, telemetry readiness, health gaps — without generating monitoring configs:

```bash
k8s-obs scan /path/to/my-k8s-repo
k8s-obs scan --github https://github.com/org/infra
```

This prints:

- Discovered resources & relationships
- Workload classification results (archetype, profile, confidence)
- Telemetry capability inference (exporter detected / not detected)
- Observability readiness verdict per workload (READY / PARTIAL / NOT READY)
- Health gaps (missing probes, resource limits, exporters)

### 6. Interpret the readiness verdicts

The agent checks whether each workload's domain metrics are actually collectible from the manifests:

| Verdict | Meaning | Action |
|---------|---------|--------|
| **READY** | Metrics exporter present + Prometheus scrape path configured | Alerts will fire — no changes needed |
| **PARTIAL** | Exporter *or* scrape config present, but not both | Add the missing piece (e.g. `prometheus.io/scrape: "true"` annotation, or deploy the exporter sidecar) |
| **NOT READY** | No metrics exposure detected in manifests | Deploy the recommended exporter sidecar and add scrape configuration |

Signals marked **CONDITIONAL** in the output include a specific remediation action, e.g.:

```
⚠ CONDITIONAL — not collectable: deploy postgres_exporter sidecar
```

### 7. Common workflows

**Review a microservices platform for monitoring gaps:**

```bash
k8s-obs scan --github https://github.com/myorg/platform
# Look at the "Observability Readiness" section — which workloads need exporters?
```

**Generate a full monitoring stack for a repo:**

```bash
k8s-obs analyze --github https://github.com/myorg/platform -o monitoring/ -v
# -v shows the agent's reasoning as it inspects each workload
```

**Check a single namespace's manifests:**

```bash
k8s-obs scan ./deploy/staging/
```

**Use a different Claude model:**

```bash
k8s-obs analyze ./infra --model claude-sonnet-4-20250514
```

### 8. Validate on a live cluster

After generating a plan, validate that everything actually works.

**Option A — CA certificate (recommended for Kind/local clusters with custom CA):**

```bash
k8s-obs validate \
  --plan observability-output/plan.json \
  --prometheus-url https://prometheus.local \
  --grafana-url https://grafana.local \
  --ca-cert /path/to/kind-cluster/tls/ca.crt \
  --allow-writes
```

**Option B — Port-forward (when self-signed certs are used or no CA exists):**

```bash
# Port-forward Prometheus and Grafana
kubectl port-forward -n monitoring svc/prometheus-server 9090:80 &
kubectl port-forward -n monitoring svc/grafana 3000:80 &

# Validate with the generated plan
k8s-obs validate \
  --plan observability-output/plan.json \
  --prometheus-url http://localhost:9090 \
  --grafana-url http://localhost:3000

# Let the agent fix issues it finds
k8s-obs validate \
  --plan observability-output/plan.json \
  --prometheus-url http://localhost:9090 \
  --grafana-url http://localhost:3000 \
  --allow-writes
```

The validation report shows:
- Which scrape targets are up/down
- Which expected metrics are present/missing
- Whether alert expressions evaluate correctly
- Which dashboards were imported
- What fixes were applied

### 9. HTML report (in-cluster)

With `--allow-writes`, the agent deploys the HTML report as a pod on the cluster, exposed via Istio at `http://report.local`:

```bash
k8s-obs validate \
  --plan observability-output/plan.json \
  --prometheus-url https://prometheus.local \
  --grafana-url https://grafana.local \
  --ca-cert tls/ca.crt \
  --allow-writes
```

This creates:

| Resource | Namespace | Purpose |
|----------|-----------|--------|
| Namespace `observability-report` | — | Isolated namespace with `istio-injection: enabled` |
| ConfigMap `report-html` | `observability-report` | Holds the rendered HTML |
| Deployment `obs-report` | `observability-report` | nginx:1-alpine serving the HTML |
| Service `obs-report` | `observability-report` | ClusterIP → nginx port 80 |
| Gateway `report-gateway` | `observability-report` | Istio ingress for `report.local` |
| VirtualService `report-vs` | `observability-report` | Routes `report.local` → nginx |

The agent auto-detects the Istio ingress gateway external IP and adds it to `/etc/hosts`:

```
172.18.0.5  report.local
```

Open `http://report.local` in your browser to see the full report.

Without `--allow-writes`, the HTML is still saved locally at `observability-output/report.html`.

## License

MIT
