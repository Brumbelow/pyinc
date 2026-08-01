"""Shared policy for checking an installed version against a declared spec.

Dependency checking and requirement evaluation must agree on what an
installed version satisfies, so both route through this one helper: an
already-installed pre-release counts (pip's own installed-version
semantics), and an unsupported or unparseable constraint is ambiguous
rather than guessed.
"""

from __future__ import annotations

from pyinc.integrations._pep440 import parse_specifier_set, satisfies


def check_version_constraints(declared_spec: str, installed_version: str) -> tuple[str, str]:
    spec_set = parse_specifier_set(declared_spec)
    if spec_set is None:
        return "ambiguous", f"cannot parse specifier: {declared_spec}"
    ok, detail = satisfies(spec_set, installed_version, include_prerelease=True)
    if not ok:
        if "unparseable" in detail or "cannot evaluate" in detail:
            return "ambiguous", detail
        return "version_mismatch", detail
    return "satisfied", detail
