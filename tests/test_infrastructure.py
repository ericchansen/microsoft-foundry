from __future__ import annotations

import re

import yaml


def test_cloud_jobs_require_explicit_enablement(repo_root):
    checked = []
    gate = "vars.CONTOSO_DEPLOYMENTS_ENABLED == 'true'"
    for path in (repo_root / ".github" / "workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            environment = job.get("environment")
            if isinstance(environment, str) and environment.startswith(("contoso-", "concierge-")):
                assert job.get("if", "").split(" && ", 1)[0] == gate, (path, name)
                checked.append((path.name, name))
    assert checked


def test_all_external_github_actions_are_commit_pinned(repo_root):
    for path in (repo_root / ".github" / "workflows").glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("uses:", "- uses:")) or "uses: ./" in stripped:
                continue
            reference = stripped.split("uses:", maxsplit=1)[1].strip().split()[0]
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (path, reference)
