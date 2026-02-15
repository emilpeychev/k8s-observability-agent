"""Shared test fixtures."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal fake K8s repo with sample manifests."""
    manifests = tmp_path / "k8s"
    manifests.mkdir()

    # Deployment
    (manifests / "deployment.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: web-app
          namespace: production
          labels:
            app: web-app
        spec:
          replicas: 3
          selector:
            matchLabels:
              app: web-app
          template:
            metadata:
              labels:
                app: web-app
            spec:
              containers:
                - name: nginx
                  image: nginx:1.25
                  ports:
                    - containerPort: 80
                  resources:
                    requests:
                      cpu: 100m
                      memory: 128Mi
                    limits:
                      cpu: 500m
                      memory: 256Mi
                  livenessProbe:
                    httpGet:
                      path: /healthz
                      port: 80
                  readinessProbe:
                    httpGet:
                      path: /ready
                      port: 80
        """)
    )

    # Service
    (manifests / "service.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: v1
        kind: Service
        metadata:
          name: web-app-svc
          namespace: production
        spec:
          type: ClusterIP
          selector:
            app: web-app
          ports:
            - port: 80
              targetPort: 80
              protocol: TCP
        """)
    )

    # Ingress
    (manifests / "ingress.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: web-app-ingress
          namespace: production
        spec:
          rules:
            - host: web.example.com
              http:
                paths:
                  - path: /
                    pathType: Prefix
                    backend:
                      service:
                        name: web-app-svc
                        port:
                          number: 80
        """)
    )

    # ConfigMap
    (manifests / "configmap.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: web-config
          namespace: production
        data:
          APP_ENV: production
        """)
    )

    # Deployment without probes (to test gap detection)
    (manifests / "worker.yaml").write_text(
        textwrap.dedent("""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: worker
          namespace: production
          labels:
            app: worker
        spec:
          replicas: 2
          selector:
            matchLabels:
              app: worker
          template:
            metadata:
              labels:
                app: worker
            spec:
              containers:
                - name: worker
                  image: myapp/worker:latest
        """)
    )

    # Non-K8s YAML (should be skipped)
    (manifests / "random.yaml").write_text("not_a_k8s_resource: true\n")

    return tmp_path
