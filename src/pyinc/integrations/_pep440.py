from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeAlias

SpecifierSet: TypeAlias = tuple[tuple[str, str], ...]


_VERSION_PAT = (
    r"^"
    r"(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:"
    r"[-_.]?"
    r"(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)"
    r"[-_.]?"
    r"(?P<pre_n>[0-9]+)?"
    r")?"
    r"(?P<post>"
    r"(?:-(?P<post_n1>[0-9]+))"
    r"|"
    r"(?:"
    r"[-_.]?"
    r"(?P<post_l>post|rev|r)"
    r"[-_.]?"
    r"(?P<post_n2>[0-9]+)?"
    r")"
    r")?"
    r"(?:"
    r"[-_.]?"
    r"(?P<dev_l>dev)"
    r"[-_.]?"
    r"(?P<dev_n>[0-9]+)?"
    r")?"
    r"(?:\+(?P<local>[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*))?"
    r"$"
)


def _canonical_pre(letter: str) -> str | None:
    if letter in ("a", "alpha"):
        return "a"
    if letter in ("b", "beta"):
        return "b"
    if letter in ("c", "rc", "pre", "preview"):
        return "rc"
    return None


@dataclass(frozen=True)
class Version:
    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[str | int, ...]


def parse_version(text: str) -> Version | None:
    stripped = text.strip().lower()
    if stripped.startswith("v"):
        stripped = stripped[1:]
    match = re.match(_VERSION_PAT, stripped)
    if match is None:
        return None

    epoch = int(match.group("epoch")) if match.group("epoch") else 0
    release = tuple(int(part) for part in match.group("release").split("."))

    pre_letter = match.group("pre_l")
    if pre_letter is not None:
        canonical = _canonical_pre(pre_letter)
        if canonical is None:
            return None
        pre_number = int(match.group("pre_n")) if match.group("pre_n") else 0
        pre: tuple[str, int] | None = (canonical, pre_number)
    else:
        pre = None

    post: int | None
    if match.group("post_n1") is not None:
        post = int(match.group("post_n1"))
    elif match.group("post_l") is not None:
        post = int(match.group("post_n2")) if match.group("post_n2") else 0
    else:
        post = None

    dev: int | None
    if match.group("dev_l") is not None:
        dev = int(match.group("dev_n")) if match.group("dev_n") else 0
    else:
        dev = None

    local_raw = match.group("local")
    if local_raw is None:
        local: tuple[str | int, ...] = ()
    else:
        parts: list[str | int] = []
        for component in re.split(r"[-_.]", local_raw):
            parts.append(int(component) if component.isdigit() else component)
        local = tuple(parts)

    return Version(
        epoch=epoch,
        release=release,
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


def _comparison_key(
    version: Version,
) -> tuple[
    int,
    tuple[int, ...],
    tuple[int, str, int],
    tuple[int, int],
    tuple[int, int],
    tuple[tuple[int, object], ...],
]:
    release = _trim_trailing_zeros(version.release)

    if version.pre is None and version.post is None and version.dev is not None:
        # A development release without an explicit pre-release segment is the
        # earliest release for its base: 1.0.dev1 < 1.0a1.
        pre_key = (-1, "", 0)
    elif version.pre is None:
        pre_key = (1, "", 0)
    else:
        letter, number = version.pre
        pre_key = (0, letter, number)

    post_key = (0, 0) if version.post is None else (1, version.post)
    dev_key = (1, 0) if version.dev is None else (0, version.dev)
    local_key = tuple(
        (1, component) if isinstance(component, int) else (0, component)
        for component in version.local
    )

    return (version.epoch, release, pre_key, post_key, dev_key, local_key)


def _trim_trailing_zeros(release: tuple[int, ...]) -> tuple[int, ...]:
    end = len(release)
    while end > 1 and release[end - 1] == 0:
        end -= 1
    return release[:end]


def compare_versions(left: Version, right: Version) -> int:
    left_key = _comparison_key(left)
    right_key = _comparison_key(right)
    left_release = left_key[1]
    right_release = right_key[1]
    width = max(len(left_release), len(right_release))
    padded_left = (
        left_key[0],
        left_release + (0,) * (width - len(left_release)),
        left_key[2],
        left_key[3],
        left_key[4],
        left_key[5],
    )
    padded_right = (
        right_key[0],
        right_release + (0,) * (width - len(right_release)),
        right_key[2],
        right_key[3],
        right_key[4],
        right_key[5],
    )
    if padded_left < padded_right:
        return -1
    if padded_left > padded_right:
        return 1
    return 0


_SPEC_PAT = r"^\s*(===|~=|==|!=|<=|>=|<|>)\s*(.+?)\s*$"


def parse_specifier_set(text: str) -> SpecifierSet | None:
    stripped = text.strip()
    if not stripped:
        return ()
    specs: list[tuple[str, str]] = []
    for clause in stripped.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(_SPEC_PAT, clause)
        if match is None:
            return None
        specs.append((match.group(1), match.group(2).strip()))
    return tuple(specs)


def _satisfies_single(
    operator: str,
    spec_version_str: str,
    version: Version,
) -> bool | None:
    """Return whether one clause matches, or ``None`` when it is unsupported.

    ``===`` never reaches here: `satisfies` evaluates arbitrary equality against
    the raw version string before parsing, since that operator is defined on the
    version as written.
    """
    is_wildcard = spec_version_str.endswith(".*")
    if is_wildcard:
        base_str = spec_version_str[:-2]
        spec_version = parse_version(base_str)
        if spec_version is None:
            return None
        if operator not in ("==", "!="):
            return None
        prefix = spec_version.release
        installed_prefix = version.release[: len(prefix)]
        padded_prefix = prefix + (0,) * max(0, len(installed_prefix) - len(prefix))
        padded_installed = installed_prefix + (0,) * max(
            0, len(padded_prefix) - len(installed_prefix)
        )
        matches = padded_installed == padded_prefix
        return matches if operator == "==" else not matches

    spec_version = parse_version(spec_version_str)
    if spec_version is None:
        return None

    if operator == "~=":
        if len(spec_version.release) < 2:
            return None
        lower = spec_version
        upper_release = spec_version.release[:-1]
        upper_release = upper_release[:-1] + (upper_release[-1] + 1,)
        upper = Version(
            epoch=spec_version.epoch,
            release=upper_release,
            pre=None,
            post=None,
            dev=None,
            local=(),
        )
        public_version = _without_local(version)
        return (
            compare_versions(public_version, lower) >= 0
            and compare_versions(public_version, upper) < 0
        )

    comparison = compare_versions(version, spec_version)
    if operator == "==":
        if spec_version.local:
            return comparison == 0
        stripped_version = Version(
            epoch=version.epoch,
            release=version.release,
            pre=version.pre,
            post=version.post,
            dev=version.dev,
            local=(),
        )
        return compare_versions(stripped_version, spec_version) == 0
    if operator == "!=":
        if spec_version.local:
            return comparison != 0
        stripped_version = Version(
            epoch=version.epoch,
            release=version.release,
            pre=version.pre,
            post=version.post,
            dev=version.dev,
            local=(),
        )
        return compare_versions(stripped_version, spec_version) != 0
    if operator == ">=":
        return compare_versions(_without_local(version), spec_version) >= 0
    if operator == "<=":
        return compare_versions(_without_local(version), spec_version) <= 0
    if operator == ">":
        if comparison <= 0:
            return False
        if version.post is not None and spec_version.post is None:
            post_base = Version(
                epoch=version.epoch,
                release=version.release,
                pre=version.pre,
                post=None,
                dev=None,
                local=(),
            )
            if compare_versions(post_base, spec_version) == 0:
                return False
        return not (version.local and compare_versions(_without_local(version), spec_version) == 0)
    if operator == "<":
        if comparison >= 0:
            return False
        is_spec_prerelease = spec_version.pre is not None or spec_version.dev is not None
        is_version_prerelease = version.pre is not None or version.dev is not None
        if not is_spec_prerelease and is_version_prerelease:
            earliest_prerelease = Version(
                epoch=spec_version.epoch,
                release=spec_version.release,
                pre=spec_version.pre,
                post=spec_version.post,
                dev=0,
                local=(),
            )
            if compare_versions(version, earliest_prerelease) >= 0:
                return False
        return True
    return None


def _without_local(version: Version) -> Version:
    if not version.local:
        return version
    return Version(
        epoch=version.epoch,
        release=version.release,
        pre=version.pre,
        post=version.post,
        dev=version.dev,
        local=(),
    )


def _specifier_allows_prereleases(spec_set: SpecifierSet) -> bool:
    for _operator, version in spec_set:
        base = version[:-2] if version.endswith(".*") else version
        parsed = parse_version(base)
        if parsed is not None and (parsed.pre is not None or parsed.dev is not None):
            return True
    return False


def satisfies(
    spec_set: SpecifierSet,
    version_str: str,
    *,
    include_prerelease: bool,
) -> tuple[bool, str]:
    # PEP 440 arbitrary equality compares the version exactly as written, so it
    # is evaluated before parsing: `===` exists precisely for versions that do
    # not conform to this specification. Pre-release exclusion is a
    # version-matching rule and does not apply to an exact string match either,
    # so an all-`===` specifier set is answered here in full.
    for operator, spec_version in spec_set:
        if operator == "===" and version_str != spec_version:
            return False, f"{version_str} does not satisfy ==={spec_version}"
    remaining = tuple((op, spec) for op, spec in spec_set if op != "===")
    if spec_set and not remaining:
        joined = ",".join(f"{op}{spec}" for op, spec in spec_set)
        return True, f"{version_str} satisfies {joined}"

    version = parse_version(version_str)
    if version is None:
        return False, f"unparseable version: {version_str}"

    if not spec_set:
        return True, f"{version_str} satisfies (no constraint)"

    is_prerelease = version.pre is not None or version.dev is not None
    if is_prerelease and not include_prerelease and not _specifier_allows_prereleases(spec_set):
        return (
            False,
            f"pre-release {version_str} excluded by default; specifier does not opt in",
        )

    for operator, spec_version in remaining:
        result = _satisfies_single(operator, spec_version, version)
        if result is None:
            return False, f"cannot evaluate: {operator}{spec_version}"
        if not result:
            return False, f"{version_str} does not satisfy {operator}{spec_version}"

    joined = ",".join(f"{operator}{version}" for operator, version in spec_set)
    return True, f"{version_str} satisfies {joined}"


__all__ = [
    "SpecifierSet",
    "Version",
    "compare_versions",
    "parse_specifier_set",
    "parse_version",
    "satisfies",
]
