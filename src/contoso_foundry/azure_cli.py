"""Thin, read-only-by-default wrapper around the Azure CLI.

Using ``az`` instead of the management SDKs keeps the dependency surface to two
packages and means the scripts authenticate exactly the way a human does, so
"it worked on my machine" and "it worked in the script" mean the same thing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

#: Verbs that mutate Azure. Any command using one must pass ``allow_write=True``,
#: which the ownership-boundary check gates on.
_WRITE_VERBS = frozenset({"create", "delete", "update", "set", "add", "remove", "purge"})


class AzureCliError(RuntimeError):
    """Raised when ``az`` is missing, unauthenticated, or returns non-zero."""


class AzureCliUnavailable(AzureCliError):
    """Raised when ``az`` is not installed or not signed in."""


def executable() -> str | None:
    """Absolute path to the Azure CLI launcher.

    On Windows ``az`` is a ``.cmd`` shim rather than an executable, so
    ``subprocess`` cannot resolve the bare name without a shell. Resolving it
    here keeps ``shell=False`` — the args are never re-parsed by a command
    interpreter.
    """
    return shutil.which("az")


def is_available() -> bool:
    return executable() is not None


def run(args: list[str], *, allow_write: bool = False, timeout: int = 300) -> Any:
    """Run ``az <args> -o json`` and return the parsed result.

    Refuses mutating verbs unless ``allow_write`` is explicitly set, so an
    accidental ``create`` cannot slip through a discovery code path.
    """
    if not allow_write:
        offending = _WRITE_VERBS.intersection(args)
        if offending:
            raise AzureCliError(
                f"refusing to run a mutating az command from a read-only call site: {sorted(offending)}"
            )
    launcher = executable()
    if launcher is None:
        raise AzureCliUnavailable("the Azure CLI ('az') is not on PATH")

    proc = subprocess.run(  # noqa: S603 - args are constructed, never shell-interpolated
        [launcher, *args, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "az login" in stderr or "AADSTS" in stderr:
            raise AzureCliUnavailable(f"az is not signed in: {stderr}")
        raise AzureCliError(f"az {' '.join(args)} failed: {stderr}")
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)


def try_run(args: list[str], *, default: Any = None, timeout: int = 300) -> Any:
    """Read-only ``run`` that returns ``default`` instead of raising.

    Used for per-region probes where a single unsupported region should narrow
    the candidate list rather than abort the whole sweep.
    """
    try:
        return run(args, timeout=timeout)
    except AzureCliError:
        return default
