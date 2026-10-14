from __future__ import annotations

import re


def test_all_external_github_actions_are_commit_pinned(repo_root):
    for path in (repo_root / ".github" / "workflows").glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("uses:", "- uses:")) or "uses: ./" in stripped:
                continue
            reference = stripped.split("uses:", maxsplit=1)[1].strip().split()[0]
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (path, reference)
