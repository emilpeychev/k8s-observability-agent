# k8s-observability-agent

AI-powered agent that reverse-engineers Kubernetes platforms from Git repositories and designs comprehensive, **technology-specific** observability solutions.

Unlike generic "add CPU/memory alerts" tools, this agent **fingerprints your workloads** — it knows that a PostgreSQL StatefulSet needs replication-lag alerts and WAL archive panels, while a Kafka cluster needs consumer-lag tracking and under-replicated partition alerts. The difference between generic and intelligent observability output comes from the classification layer, not the LLM.

## What it does

1. **Scans** a Git repository for Kubernetes manifests (Deployments, Services, StatefulSets, Ingresses, ConfigMaps, etc.)
2. **Classifies** every container image into a technology profile using evidence accumulation — image regex, ports, environment variables, and Kubernetes labels all contribute a weighted confidence score
3. **Analyses** the platform — discovers resource relationships (Service→Deployment, Ingress→Service, HPA→target), identifies health/monitoring gaps including missing exporters
4. **Generates** a tailored observability plan using Claude AI, including:
   - Prometheus alerting rules (`.yml`) — technology-specific, not generic
   - Grafana dashboard definitions (`.json`) — grouped by archetype
   - A human-readable Markdown summary with best-practice recommendations

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

### CLI options

```
k8s-obs analyze --help

Options:
  --github TEXT       Clone from a GitHub URL instead of a local path
  --branch TEXT       Git branch to checkout (default: main)
  -o, --output TEXT   Output directory (default: observability-output)
  --model TEXT        Anthropic model to use
  --api-key TEXT      Anthropic API key (or set ANTHROPIC_API_KEY)
  --max-turns INT     Maximum agent reasoning turns (default: 30)
  -v, --verbose       Show full agent reasoning
```

## Output

The agent writes to the output directory (default: `observability-output/`):

| File | Description |
|------|-------------|
| `prometheus-rules.yml` | Prometheus alerting rules — technology-specific PromQL |
| `grafana-*.json` | Grafana dashboard JSON definitions grouped by archetype |
| `observability-plan.md` | Human-readable summary with all recommendations |

## Architecture

```
agent/
  cli.py          # Click CLI entry-point (k8s-obs command)
  config.py       # Settings & configuration
  models.py       # Pydantic models for K8s resources & observability plans
  scanner.py      # Git repo scanning & K8s manifest parsing
  classifier.py   # Workload fingerprinting engine (archetype → profile → metrics)
  analyzer.py     # Platform relationship analysis & gap detection
  core.py         # Claude agent loop with tool calling
  renderer.py     # Jinja2 template rendering for outputs
  tools/
    registry.py   # Tool definitions & implementations for the agent
  templates/
    prometheus_rules.yml.j2
    grafana_dashboard.json.j2
    plan_summary.md.j2
tests/
  conftest.py           # Shared fixtures
  test_models.py        # Model unit tests
  test_scanner.py       # Scanner tests
  test_classifier.py    # Classifier tests (knowledge system validation)
  test_analyzer.py      # Analyzer tests
  test_tools.py         # Tool registry tests
  test_renderer.py      # Renderer tests
```

### Pipeline

```
Git Repo → Scanner → Classifier → Analyzer → LLM Agent → Renderer → Output
              ↓           ↓           ↓           ↓
          K8s manifests  Archetype   Relationships  Observability
          parsed         profiles    & gaps         plan (structured)
                         assigned    detected       
```

The classifier is the **sensor** — if it's wrong, the LLM reasons on false facts and produces polished but incorrect output. The classifier tests validate that the system interprets infrastructure the same way a human SRE would.

## How the agent works

The agent uses Claude's tool-calling capability in an agentic loop:

1. The platform summary is sent as context to Claude
2. Claude calls tools to explore resources, check gaps, and retrieve **archetype-specific observability knowledge**
3. After thorough analysis, Claude calls `generate_observability_plan` with structured data
4. The structured plan is rendered into Prometheus rules, Grafana dashboards, and a Markdown report

Available tools for the agent:

| Tool | Purpose |
|------|---------|
| `list_resources` | List/filter discovered K8s resources |
| `get_resource_detail` | Inspect a specific resource (containers, probes, limits, labels) |
| `get_relationships` | View Service→Deployment, Ingress→Service, HPA→target mappings |
| `get_platform_summary` | High-level platform overview with resource counts |
| `check_health_gaps` | Missing probes, resource limits, orphaned selectors, **missing exporters** |
| `get_workload_insights` | **Key tool** — returns golden metrics, alert rules, exporter requirements, and recommendations per classified workload |
| `generate_observability_plan` | Submit the final structured plan |

The LLM's system prompt instructs it to modulate output based on classification **score** — high-confidence workloads get technology-specific alerts, low-confidence ones get generic Kubernetes metrics with a recommendation to verify.

## Development

```bash
pip install -e ".[dev]"
pytest                     # 94 tests
ruff check agent/ tests/   # lint
```

## License

MIT
