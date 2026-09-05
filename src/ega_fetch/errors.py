"""Typed exceptions. Each maps to one documented exit code.

The base classes are deliberately left as plain :class:`Exception`, matching the
original module. Re-parenting them onto built-ins such as :class:`ValueError`
would change which ``except`` clauses catch them, and this package's contract is
that behaviour is unchanged.
"""

from __future__ import annotations

from typing import List, Optional


class ConfigError(Exception):
    """Usage or configuration problem. Exit 2."""


class PreflightError(Exception):
    """Environment is not fit to run. Exit 3.

    Carries the problems as data plus an optional remediation hint; the CLI
    layer owns how they are rendered, so no raise site formats bullets.

    Args:
        *problems: One message per distinct problem. Empty strings are dropped.
        hint: Optional single remediation line, rendered after the bullets.
    """

    def __init__(self, *problems: str, hint: Optional[str] = None) -> None:
        self.problems: List[str] = [p for p in problems if p]
        self.hint = hint
        super().__init__("; ".join(self.problems))

    def render(self) -> str:
        """Return the problems as an indented bullet list, hint last."""
        out = [f"  - {p}" for p in self.problems]
        if self.hint:
            out.append(f"    {self.hint}")
        return "\n".join(out)


class LockBusy(Exception):
    """Another run holds this output directory. Exit 4.

    Distinct from :class:`ConfigError`: this is transient and worth retrying,
    whereas bad arguments never are.
    """


class FileFailure(Exception):
    """One file could not be converged. Other files continue."""


class Cancelled(Exception):
    """Cooperative stop raised from inside a blocking step. Exit 130."""
