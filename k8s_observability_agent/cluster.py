"""Kubernetes cluster interaction via kubectl subprocess calls.

This module provides a safe interface to a live cluster for the validation
agent.  All calls go through ``kubectl`` so the agent uses whatever
kubeconfig / context the operator has active — no in-process K8s client
libraries needed.

Safety:
  • Read-only operations (get, describe, logs) are unrestricted.
  • Write operations (apply, delete) require explicit opt-in via
    ``ClusterClient(allow_writes=True)``.
  • All commands are logged for auditability.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum output we'll capture from kubectl to avoid memory blowup.
_MAX_OUTPUT_BYTES = 512 * 1024  # 512 KB


@dataclass
class CommandResult:
    """Result of a kubectl command execution."""

    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def summary(self) -> str:
        if self.ok:
            return self.stdout[:2000] if len(self.stdout) > 2000 else self.stdout
        return f"ERROR (rc={self.returncode}): {self.stderr[:1000]}"


@dataclass
class ClusterClient:
    """Interface to a Kubernetes cluster via kubectl.

    Parameters
    ----------
    kubeconfig : str
        Path to kubeconfig file.  Empty string means use the default.
    context : str
        Kubernetes context to use.  Empty string means use the current context.
    allow_writes : bool
        If False (default), ``apply`` and ``delete`` operations are rejected.
    """

    kubeconfig: str = ""
    context: str = ""
    allow_writes: bool = False
    _base_cmd: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._base_cmd = ["kubectl"]
        if self.kubeconfig:
            self._base_cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            self._base_cmd += ["--context", self.context]

    # ── Low-level executor ────────────────────────────────────────────────

    def _run(self, args: list[str], timeout: int = 30) -> CommandResult:
        """Run a kubectl command and return the result."""
        cmd = self._base_cmd + args
        cmd_str = shlex.join(cmd)
        logger.info("kubectl: %s", cmd_str)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            # Truncate very large output
            stdout = proc.stdout[:_MAX_OUTPUT_BYTES]
            stderr = proc.stderr[:_MAX_OUTPUT_BYTES]
            return CommandResult(
                command=cmd_str,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                command=cmd_str,
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
            )
        except FileNotFoundError:
            return CommandResult(
                command=cmd_str,
                returncode=-1,
                stdout="",
                stderr="kubectl not found. Is it installed and on the PATH?",
            )

    # ── Read operations ───────────────────────────────────────────────────

    def get_current_context(self) -> CommandResult:
        """Return the current kubectl context."""
        return self._run(["config", "current-context"])

    def cluster_info(self) -> CommandResult:
        """Return cluster info (API server URL, etc.)."""
        return self._run(["cluster-info"])

    def get_namespaces(self) -> CommandResult:
        """List all namespaces."""
        return self._run(["get", "namespaces", "-o", "json"])

    def get_resources(
        self,
        kind: str,
        namespace: str = "",
        label_selector: str = "",
    ) -> CommandResult:
        """Get resources of a given kind."""
        args = ["get", kind, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        else:
            args += ["--all-namespaces"]
        if label_selector:
            args += ["-l", label_selector]
        return self._run(args)

    def describe_resource(
        self, kind: str, name: str, namespace: str = "default"
    ) -> CommandResult:
        """Describe a specific resource (events, status, etc.)."""
        return self._run(["describe", kind, name, "-n", namespace])

    def get_pod_logs(
        self,
        pod_name: str,
        namespace: str = "default",
        container: str = "",
        tail_lines: int = 100,
    ) -> CommandResult:
        """Get recent logs from a pod."""
        args = ["logs", pod_name, "-n", namespace, f"--tail={tail_lines}"]
        if container:
            args += ["-c", container]
        return self._run(args, timeout=15)

    def get_endpoints(self, service_name: str, namespace: str = "default") -> CommandResult:
        """Get endpoints for a service."""
        return self._run(["get", "endpoints", service_name, "-n", namespace, "-o", "json"])

    def get_events(self, namespace: str = "default", field_selector: str = "") -> CommandResult:
        """Get events in a namespace."""
        args = ["get", "events", "-n", namespace, "-o", "json", "--sort-by=.lastTimestamp"]
        if field_selector:
            args += [f"--field-selector={field_selector}"]
        return self._run(args)

    def top_pods(self, namespace: str = "") -> CommandResult:
        """Get pod resource usage via metrics-server."""
        args = ["top", "pods"]
        if namespace:
            args += ["-n", namespace]
        else:
            args += ["--all-namespaces"]
        return self._run(args, timeout=15)

    def check_connectivity(self) -> CommandResult:
        """Quick check that kubectl can reach the cluster."""
        return self._run(["version", "--short"], timeout=10)

    # ── Write operations (guarded) ────────────────────────────────────────

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise PermissionError(
                "Write operations are disabled.  "
                "Pass --allow-writes to enable applying changes to the cluster."
            )

    def apply_manifest(self, manifest_yaml: str, namespace: str = "default") -> CommandResult:
        """Apply a YAML manifest to the cluster."""
        self._require_writes()
        cmd = self._base_cmd + ["apply", "-f", "-", "-n", namespace]
        cmd_str = shlex.join(cmd)
        logger.info("kubectl apply (stdin): namespace=%s", namespace)

        try:
            proc = subprocess.run(
                cmd,
                input=manifest_yaml,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return CommandResult(
                command=cmd_str,
                returncode=proc.returncode,
                stdout=proc.stdout[:_MAX_OUTPUT_BYTES],
                stderr=proc.stderr[:_MAX_OUTPUT_BYTES],
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                command=cmd_str, returncode=-1, stdout="", stderr="Apply timed out after 30s"
            )

    def delete_resource(
        self, kind: str, name: str, namespace: str = "default"
    ) -> CommandResult:
        """Delete a specific resource."""
        self._require_writes()
        return self._run(["delete", kind, name, "-n", namespace])

    # ── Convenience helpers ───────────────────────────────────────────────

    def find_prometheus(self) -> dict[str, Any]:
        """Try to locate Prometheus in the cluster.

        Searches for pods matching common Prometheus labels/names and
        returns connection info.
        """
        # Try common selectors
        for label in [
            "app=prometheus",
            "app.kubernetes.io/name=prometheus",
            "app=kube-prometheus-stack-prometheus",
            "app.kubernetes.io/component=prometheus",
        ]:
            result = self.get_resources("service", label_selector=label)
            if result.ok:
                try:
                    data = json.loads(result.stdout)
                    items = data.get("items", [])
                    if items:
                        svc = items[0]
                        ns = svc["metadata"]["namespace"]
                        name = svc["metadata"]["name"]
                        port = 9090
                        for p in svc.get("spec", {}).get("ports", []):
                            if p.get("name") in ("http-web", "web", "http", "prometheus"):
                                port = p.get("port", 9090)
                                break
                            port = p.get("port", 9090)
                        return {
                            "found": True,
                            "namespace": ns,
                            "service": name,
                            "port": port,
                            "url": f"http://{name}.{ns}.svc.cluster.local:{port}",
                            "label": label,
                        }
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        return {"found": False, "reason": "No Prometheus service found in the cluster"}

    def find_grafana(self) -> dict[str, Any]:
        """Try to locate Grafana in the cluster."""
        for label in [
            "app=grafana",
            "app.kubernetes.io/name=grafana",
            "app=kube-prometheus-stack-grafana",
        ]:
            result = self.get_resources("service", label_selector=label)
            if result.ok:
                try:
                    data = json.loads(result.stdout)
                    items = data.get("items", [])
                    if items:
                        svc = items[0]
                        ns = svc["metadata"]["namespace"]
                        name = svc["metadata"]["name"]
                        port = 3000
                        for p in svc.get("spec", {}).get("ports", []):
                            port = p.get("port", 3000)
                            break
                        return {
                            "found": True,
                            "namespace": ns,
                            "service": name,
                            "port": port,
                            "url": f"http://{name}.{ns}.svc.cluster.local:{port}",
                            "label": label,
                        }
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        return {"found": False, "reason": "No Grafana service found in the cluster"}
