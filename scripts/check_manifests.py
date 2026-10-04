#!/usr/bin/env python3
"""Manifest rules for the rendered production overlay (Kyverno policies + public-repo boundary).

Usage: kustomize build infra/k8s/production > render.yaml && python scripts/check_manifests.py render.yaml

Checks what the cluster's admission policies enforce (images from ghcr.io/madfam-org pinned by
digest, non-root, no privilege escalation, capabilities drop ALL, read-only root, probes, requests
and memory limits) plus this repo's own rules: no Namespace/Ingress objects (Enclii owns them), no
Secret or ExternalSecret objects (no secret material or store paths in a public repo), and the tunnel
may reach only the API and the landing pods. The worker (no HTTP port) must have no Service and no ingress.
"""

from __future__ import annotations

import sys

import yaml

ALLOWED_TUNNEL_TARGETS = {"fabrication-prep-api", "fabrication-prep-web"}


def check(docs: list[dict]) -> list[str]:
    errors: list[str] = []
    kinds = [d.get("kind") for d in docs]
    for forbidden in ("Namespace", "Ingress", "Secret", "ExternalSecret"):
        if forbidden in kinds:
            errors.append(f"{forbidden} objects do not belong in this overlay")
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    if len(deployments) != 3:
        errors.append(f"expected 3 Deployments, found {len(deployments)}")
    for dep in deployments:
        name = dep["metadata"]["name"]
        pod = dep["spec"]["template"]["spec"]
        if not pod.get("securityContext", {}).get("runAsNonRoot"):
            errors.append(f"{name}: pod securityContext.runAsNonRoot must be true")
        containers = [("container", c) for c in pod.get("containers", [])]
        containers += [("initContainer", c) for c in pod.get("initContainers", [])]
        for role, c in containers:
            where = f"{name}/{c['name']}"
            image = c.get("image", "")
            if not image.startswith("ghcr.io/madfam-org/") or "@sha256:" not in image:
                errors.append(f"{where}: image must be ghcr.io/madfam-org/* pinned by digest (got {image})")
            if image.endswith(":latest"):
                errors.append(f"{where}: no :latest")
            sc = c.get("securityContext", {})
            if sc.get("allowPrivilegeEscalation") is not False or sc.get("privileged"):
                errors.append(f"{where}: allowPrivilegeEscalation must be false and privileged unset")
            if sc.get("readOnlyRootFilesystem") is not True:
                errors.append(f"{where}: readOnlyRootFilesystem must be true")
            if "ALL" not in sc.get("capabilities", {}).get("drop", []):
                errors.append(f"{where}: capabilities must drop ALL")
            res = c.get("resources", {})
            if not {"cpu", "memory"} <= set(res.get("requests", {})) or "memory" not in res.get("limits", {}):
                errors.append(f"{where}: requests (cpu, memory) and a memory limit are required")
            if role == "container" and not (c.get("livenessProbe") and c.get("readinessProbe")):
                errors.append(f"{where}: liveness and readiness probes are required")
            if c.get("ports") and any("hostPort" in p for p in c["ports"]):
                errors.append(f"{where}: no host ports")
    for svc in (d for d in docs if d.get("kind") == "Service"):
        if svc["spec"].get("selector", {}).get("app.kubernetes.io/name") == "fabrication-prep-worker":
            errors.append(f"{svc['metadata']['name']}: the worker takes no traffic and has no Service")
    for policy in (d for d in docs if d.get("kind") == "NetworkPolicy"):
        if "cloudflare-tunnel" in str(policy.get("spec", {}).get("ingress", "")):
            target = policy["spec"]["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name")
            if target not in ALLOWED_TUNNEL_TARGETS:
                errors.append(f"{policy['metadata']['name']}: the tunnel may reach only the API and landing pods")
    return errors


def main(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    errors = check(docs)
    for e in errors:
        print(f"MANIFEST RULE: {e}")
    if not errors:
        print(f"manifest rules ok ({len(docs)} objects)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
