#!/usr/bin/env python3
"""
Assert that the Secret the chart renders and the Secret the Deployment reads
are the same Secret, with the same keys.

A mismatch here renders perfectly valid YAML and only fails at pod start, as
CreateContainerConfigError — long after the thing that introduced it looked
fine in review. The auth key names are configurable (`auth.usernameKey` /
`auth.passwordKey`) precisely so a SOPS-managed Secret can use its own
conventions, which is exactly the kind of knob that drifts out of sync.

    check_chart_auth_keys.py <rendered.yaml> [...]

Reads already-rendered manifests so the caller decides which value
permutations matter.
"""
from __future__ import annotations

import sys

import yaml


def check(path: str) -> list[str]:
    with open(path) as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]

    secrets = [d for d in docs if d.get("kind") == "Secret"]
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    if not deployments:
        return [f"{path}: no Deployment rendered"]

    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    refs = [
        e["valueFrom"]["secretKeyRef"]
        for e in container.get("env", [])
        if e["name"].startswith("RH_DASHBOARD_") and "valueFrom" in e
    ]
    if not refs:
        return [f"{path}: the deployment reads no credentials from a Secret"]

    problems = []
    read_keys = {r["key"] for r in refs}
    ref_names = {r["name"] for r in refs}
    if len(ref_names) != 1:
        problems.append(f"{path}: env vars point at several Secrets: {sorted(ref_names)}")

    ref_name = sorted(ref_names)[0]
    provided = {s["metadata"]["name"]: set(s.get("stringData", {})) for s in secrets}

    if ref_name in provided:
        if provided[ref_name] != read_keys:
            problems.append(
                f"{path}: Secret {ref_name} provides {sorted(provided[ref_name])} "
                f"but the deployment reads {sorted(read_keys)}")
        else:
            print(f"  {path}: {ref_name} provides and supplies {sorted(read_keys)}")
    else:
        # An existingSecret is supplied by the operator, so the chart renders
        # no Secret of its own — the reference is all we can check here.
        print(f"  {path}: reads {sorted(read_keys)} from external Secret {ref_name}")

    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    problems: list[str] = []
    for path in argv:
        problems.extend(check(path))
    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("auth Secret keys line up in every rendered permutation")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
