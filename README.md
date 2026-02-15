# k8s-observability-agent

AI-powered agent that reverse-engineers Kubernetes platforms from Git repositories and designs comprehensive observability solutions.

## What it does

1. **Scans** a Git repository for Kubernetes manifests (Deployments, Services, Ingresses, ConfigMaps, etc.)
2. **Analyses** the platform — discovers resource relationships, identifies health/monitoring gaps
3. **Generates** a tailored observability plan using Claude AI, including:
   - Prometheus alerting rules (`.yml`)
   - Grafana dashboard definitions (`.json`)
   - A human-readable Markdown summary with best-practice recommendations

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
| `prometheus-rules.yml` | Prometheus alerting rules ready to load |
| `grafana-*.json` | Grafana dashboard JSON definitions |
| `observability-plan.md` | Human-readable summary with all recommendations |

## Architecture

```
agent/
  cli.py          # Click CLI entry-point (k8s-obs command)
  config.py       # Settings & configuration
  models.py       # Pydantic models for K8s resources & observability plans
  scanner.py      # Git repo scanning & K8s manifest parsing
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
  test_analyzer.py      # Analyzer tests
  test_tools.py         # Tool registry tests
  test_renderer.py      # Renderer tests
```

## How the agent works

The agent uses Claude's tool-calling capability in an agentic loop:

1. The platform summary is sent as context to Claude
2. Claude calls tools to explore resources, check gaps, and drill into details
3. After thorough analysis, Claude calls `generate_observability_plan` with structured data
4. The structured plan is rendered into Prometheus rules, Grafana dashboards, and a Markdown report

Available tools for the agent:
- `list_resources` — List/filter discovered K8s resources
- `get_resource_detail` — Inspect a specific resource (containers, probes, limits, etc.)
- `get_relationships` — View Service→Deployment, Ingress→Service mappings
- `get_platform_summary` — High-level platform overview
- `check_health_gaps` — Find missing probes, resource limits, orphaned selectors
- `generate_observability_plan` — Submit the final structured plan

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check agent/ tests/
```

## License

MIT

## Tests
```sh
export ANTHROPIC_API_KEY="sk-ant-..."
k8s-obs analyze /path/to/k8s-repo        # Full AI analysis
k8s-obs scan /path/to/k8s-repo           # Scan only (no AI)
k8s-obs analyze --github https://github.com/org/repo  # Remote repo
```
