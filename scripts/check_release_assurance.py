"""Fail closed until independent review and release-candidate soak are evidenced."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_REVIEW_DISCIPLINES = frozenset({"incremental-computation", "filesystem-security"})
_MINIMUM_SOAK_SECONDS = 30 * 24 * 60 * 60
_MAXIMUM_EVIDENCE_BYTES = 64 * 1024 * 1024
EvidenceFetcher = Callable[[str], bytes]


class ReleaseAssuranceError(ValueError):
    """The candidate has not satisfied the independent assurance gates."""


class _DuplicateJSONKey(ValueError):
    """A signed evidence document used an ambiguous duplicate object key."""


def _reject(message: str) -> NoReturn:
    raise ReleaseAssuranceError(message)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJSONKey(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _reject(f"{context} must be an object")
    return value


def _string(document: Mapping[str, object], key: str, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        _reject(f"{context}.{key} must be a non-empty string")
    return value


def _exact_keys(document: Mapping[str, object], expected: set[str], context: str) -> None:
    actual = set(document)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        _reject(f"{context} has invalid fields ({'; '.join(details)})")


def _strings(document: Mapping[str, object], key: str, context: str) -> tuple[str, ...]:
    value = document.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        _reject(f"{context}.{key} must be a non-empty array of non-empty strings")
    return tuple(value)


def _instant(value: object, context: str) -> datetime:
    if not isinstance(value, str):
        _reject(f"{context} must be an ISO UTC timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject(f"{context} must be an ISO UTC timestamp")
    offset = instant.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        _reject(f"{context} must be in UTC")
    return instant


def _evidence_instant(
    value: object,
    context: str,
    *,
    candidate_created: datetime,
    current: datetime,
) -> datetime:
    instant = _instant(value, context)
    if instant < candidate_created:
        _reject(f"{context} cannot predate the candidate commit")
    if instant > current:
        _reject(f"{context} cannot be in the future")
    return instant


def _fetch_https_evidence(location: str) -> bytes:
    request = urllib.request.Request(location, headers={"User-Agent": "pyinc-release-gate/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            final_location = response.geturl()
            if not final_location.startswith("https://"):
                _reject("remote evidence redirected outside HTTPS")
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None and int(declared_length) > _MAXIMUM_EVIDENCE_BYTES:
                _reject("remote evidence exceeds the 64 MiB verification limit")
            raw_payload = response.read(_MAXIMUM_EVIDENCE_BYTES + 1)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        _reject(f"cannot fetch remote evidence: {type(exc).__name__}: {exc}")
    if not isinstance(raw_payload, bytes):
        _reject("remote evidence response was not bytes")
    payload = raw_payload
    if len(payload) > _MAXIMUM_EVIDENCE_BYTES:
        _reject("remote evidence exceeds the 64 MiB verification limit")
    return payload


def _evidence_reference(
    root: Path,
    value: object,
    context: str,
    *,
    fetch_remote: EvidenceFetcher,
) -> tuple[tuple[str, str], dict[str, object]]:
    reference = _object(value, context)
    if set(reference) != {"location", "sha256"}:
        _reject(f"{context} must contain exactly location and sha256")
    location = _string(reference, "location", context)
    expected_digest = _string(reference, "sha256", context).lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        _reject(f"{context}.sha256 must be a lowercase SHA-256 digest")
    if location.startswith("https://"):
        payload = fetch_remote(location)
        if not isinstance(payload, bytes):
            _reject(f"{context} fetcher did not return bytes")
    else:
        relative = Path(location)
        if relative.is_absolute() or ".." in relative.parts:
            _reject(f"{context}.location must stay inside the repository")
        root_resolved = root.resolve()
        path = (root_resolved / relative).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError:
            _reject(f"{context}.location must stay inside the repository")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _reject(f"{context}.location cannot be read: {type(exc).__name__}: {exc}")
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != expected_digest:
        _reject(f"{context} SHA-256 does not match the referenced evidence")
    try:
        decoded: object = json.loads(payload, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJSONKey) as exc:
        _reject(f"{context} must reference a UTF-8 JSON artifact: {exc}")
    return (location, expected_digest), _object(decoded, f"{context} artifact")


def _validate_artifact_header(
    artifact: Mapping[str, object],
    *,
    context: str,
    evidence_kind: str,
    version: str,
    candidate_commit: str,
) -> None:
    if type(artifact.get("schema_version")) is not int or artifact.get("schema_version") != 1:
        _reject(f"{context}.schema_version must be 1")
    if artifact.get("evidence_kind") != evidence_kind:
        _reject(f"{context}.evidence_kind must be {evidence_kind!r}")
    if artifact.get("version") != version:
        _reject(f"{context}.version does not match the release")
    if artifact.get("candidate_commit") != candidate_commit:
        _reject(f"{context}.candidate_commit does not match the candidate")


def _validate_review_scope(
    artifact: Mapping[str, object],
    *,
    version: str,
    candidate_commit: str,
    discipline: str,
    reviewer: str,
    candidate_created: datetime,
    current: datetime,
    context: str,
) -> datetime:
    _exact_keys(
        artifact,
        {
            "schema_version",
            "evidence_kind",
            "version",
            "candidate_commit",
            "discipline",
            "reviewer",
            "recorded_utc",
            "method",
            "scope",
        },
        context,
    )
    _validate_artifact_header(
        artifact,
        context=context,
        evidence_kind="review-scope",
        version=version,
        candidate_commit=candidate_commit,
    )
    if artifact.get("discipline") != discipline:
        _reject(f"{context}.discipline does not match the review")
    if artifact.get("reviewer") != reviewer:
        _reject(f"{context}.reviewer does not match the review")
    recorded = _evidence_instant(
        artifact.get("recorded_utc"),
        f"{context}.recorded_utc",
        candidate_created=candidate_created,
        current=current,
    )
    _string(artifact, "method", context)
    _strings(artifact, "scope", context)
    return recorded


def _validate_review_report(
    artifact: Mapping[str, object],
    *,
    version: str,
    candidate_commit: str,
    discipline: str,
    reviewer: str,
    candidate_created: datetime,
    current: datetime,
    context: str,
) -> datetime:
    _exact_keys(
        artifact,
        {
            "schema_version",
            "evidence_kind",
            "version",
            "candidate_commit",
            "discipline",
            "reviewer",
            "completed_utc",
            "result",
            "open_p0_p1",
            "summary",
            "closure_evidence",
        },
        context,
    )
    _validate_artifact_header(
        artifact,
        context=context,
        evidence_kind="review-report",
        version=version,
        candidate_commit=candidate_commit,
    )
    if artifact.get("discipline") != discipline:
        _reject(f"{context}.discipline does not match the review")
    if artifact.get("reviewer") != reviewer:
        _reject(f"{context}.reviewer does not match the review")
    completed = _evidence_instant(
        artifact.get("completed_utc"),
        f"{context}.completed_utc",
        candidate_created=candidate_created,
        current=current,
    )
    if artifact.get("result") != "pass":
        _reject(f"{context}.result must be 'pass'")
    if type(artifact.get("open_p0_p1")) is not int or artifact.get("open_p0_p1") != 0:
        _reject(f"{context} must record zero open P0/P1 findings")
    _string(artifact, "summary", context)
    _strings(artifact, "closure_evidence", context)
    return completed


def _validate_counterexample_corpus(
    artifact: Mapping[str, object],
    *,
    version: str,
    candidate_commit: str,
    discipline: str,
    reviewer: str,
    candidate_created: datetime,
    current: datetime,
    context: str,
) -> datetime:
    _exact_keys(
        artifact,
        {
            "schema_version",
            "evidence_kind",
            "version",
            "candidate_commit",
            "discipline",
            "reviewer",
            "recorded_utc",
            "cases",
        },
        context,
    )
    _validate_artifact_header(
        artifact,
        context=context,
        evidence_kind="counterexample-corpus",
        version=version,
        candidate_commit=candidate_commit,
    )
    if artifact.get("discipline") != discipline:
        _reject(f"{context}.discipline does not match the review")
    if artifact.get("reviewer") != reviewer:
        _reject(f"{context}.reviewer does not match the review")
    recorded = _evidence_instant(
        artifact.get("recorded_utc"),
        f"{context}.recorded_utc",
        candidate_created=candidate_created,
        current=current,
    )
    cases = _strings(artifact, "cases", context)
    if len(set(cases)) != len(cases):
        _reject(f"{context}.cases must not contain duplicates")
    return recorded


def _validate_soak_project(
    artifact: Mapping[str, object],
    *,
    version: str,
    candidate_commit: str,
    project: str,
    candidate_created: datetime,
    declared_started: datetime,
    declared_completed: datetime,
    current: datetime,
    context: str,
) -> None:
    _exact_keys(
        artifact,
        {
            "schema_version",
            "evidence_kind",
            "version",
            "candidate_commit",
            "project",
            "started_utc",
            "completed_utc",
            "result",
            "p0_p1_failures",
            "runs",
            "environment",
            "summary",
        },
        context,
    )
    _validate_artifact_header(
        artifact,
        context=context,
        evidence_kind="soak-project",
        version=version,
        candidate_commit=candidate_commit,
    )
    if artifact.get("project") != project:
        _reject(f"{context}.project does not match the external project")
    started = _evidence_instant(
        artifact.get("started_utc"),
        f"{context}.started_utc",
        candidate_created=candidate_created,
        current=current,
    )
    completed = _evidence_instant(
        artifact.get("completed_utc"),
        f"{context}.completed_utc",
        candidate_created=candidate_created,
        current=current,
    )
    if started < declared_started or completed > declared_completed:
        _reject(f"{context} timestamps must stay inside the declared soak interval")
    if (completed - started).total_seconds() < _MINIMUM_SOAK_SECONDS:
        _reject(f"{context} must span at least 30 complete days")
    if artifact.get("result") != "pass":
        _reject(f"{context}.result must be 'pass'")
    if type(artifact.get("p0_p1_failures")) is not int or artifact.get("p0_p1_failures") != 0:
        _reject(f"{context} must record zero P0/P1 failures")
    runs = artifact.get("runs")
    if type(runs) is not int or runs <= 0:
        _reject(f"{context}.runs must be a positive integer")
    _string(artifact, "environment", context)
    _string(artifact, "summary", context)


def verify_assurance(
    document: Mapping[str, object],
    *,
    version: str,
    candidate_commit: str,
    candidate_created_epoch: int,
    root: Path,
    now_utc: datetime | None = None,
    fetch_remote: EvidenceFetcher = _fetch_https_evidence,
) -> None:
    """Validate one exact candidate's review and soak closure record."""
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        _reject(f"invalid candidate commit: {candidate_commit!r}")
    if type(candidate_created_epoch) is not int or candidate_created_epoch < 0:
        _reject("candidate creation epoch must be a non-negative integer")
    _exact_keys(
        document,
        {
            "schema_version",
            "version",
            "candidate_commit",
            "status",
            "open_p0_p1",
            "reviews",
            "soak",
        },
        "release assurance",
    )
    if type(document.get("schema_version")) is not int or document.get("schema_version") != 3:
        _reject("release assurance schema_version must be 3")
    if document.get("version") != version:
        _reject("release assurance version does not match the candidate")
    if document.get("candidate_commit") != candidate_commit:
        _reject("release assurance candidate_commit does not match the candidate")
    if document.get("status") != "ready":
        _reject("release assurance status is not ready")
    if type(document.get("open_p0_p1")) is not int or document.get("open_p0_p1") != 0:
        _reject("release assurance must record zero open P0/P1 items")

    candidate_created = datetime.fromtimestamp(candidate_created_epoch, UTC)
    current = datetime.now(UTC) if now_utc is None else now_utc
    if current.utcoffset() is None or current.utcoffset() != UTC.utcoffset(current):
        _reject("current assurance time must be in UTC")

    raw_reviews = document.get("reviews")
    if not isinstance(raw_reviews, list):
        _reject("release assurance reviews must be an array")
    reviews: dict[str, dict[str, object]] = {}
    for index, raw_review in enumerate(raw_reviews):
        review = _object(raw_review, f"reviews[{index}]")
        _exact_keys(
            review,
            {
                "discipline",
                "candidate_commit",
                "reviewer",
                "scope",
                "report",
                "counterexample_corpus",
            },
            f"reviews[{index}]",
        )
        discipline = _string(review, "discipline", f"reviews[{index}]")
        if discipline in reviews:
            _reject(f"duplicate independent review discipline: {discipline}")
        reviews[discipline] = review
    if frozenset(reviews) != _REVIEW_DISCIPLINES:
        _reject(
            "release assurance requires incremental-computation and filesystem-security reviews"
        )
    reviewer_names = {
        _string(review, "reviewer", f"review {discipline}")
        for discipline, review in reviews.items()
    }
    if len(reviewer_names) != len(reviews):
        _reject("independent reviews must name distinct reviewers")
    for discipline, review in reviews.items():
        if review.get("candidate_commit") != candidate_commit:
            _reject(f"review {discipline}.candidate_commit does not match the candidate")
        reviewer = _string(review, "reviewer", f"review {discipline}")
        _, scope_artifact = _evidence_reference(
            root,
            review.get("scope"),
            f"review {discipline}.scope",
            fetch_remote=fetch_remote,
        )
        scope_recorded = _validate_review_scope(
            scope_artifact,
            version=version,
            candidate_commit=candidate_commit,
            discipline=discipline,
            reviewer=reviewer,
            candidate_created=candidate_created,
            current=current,
            context=f"review {discipline}.scope artifact",
        )
        _, report_artifact = _evidence_reference(
            root,
            review.get("report"),
            f"review {discipline}.report",
            fetch_remote=fetch_remote,
        )
        report_completed = _validate_review_report(
            report_artifact,
            version=version,
            candidate_commit=candidate_commit,
            discipline=discipline,
            reviewer=reviewer,
            candidate_created=candidate_created,
            current=current,
            context=f"review {discipline}.report artifact",
        )
        _, corpus_artifact = _evidence_reference(
            root,
            review.get("counterexample_corpus"),
            f"review {discipline}.counterexample_corpus",
            fetch_remote=fetch_remote,
        )
        corpus_recorded = _validate_counterexample_corpus(
            corpus_artifact,
            version=version,
            candidate_commit=candidate_commit,
            discipline=discipline,
            reviewer=reviewer,
            candidate_created=candidate_created,
            current=current,
            context=f"review {discipline}.counterexample_corpus artifact",
        )
        if scope_recorded > report_completed:
            _reject(f"review {discipline} scope cannot postdate the completed report")
        if corpus_recorded > report_completed:
            _reject(f"review {discipline} corpus cannot postdate the completed report")
    soak = _object(document.get("soak"), "soak")
    _exact_keys(
        soak,
        {
            "candidate_commit",
            "started_utc",
            "completed_utc",
            "external_projects",
            "p0_p1_failures",
        },
        "soak",
    )
    if soak.get("candidate_commit") != candidate_commit:
        _reject("soak.candidate_commit does not match the candidate")
    started = _instant(soak.get("started_utc"), "soak.started_utc")
    completed = _instant(soak.get("completed_utc"), "soak.completed_utc")
    if started < candidate_created:
        _reject("release-candidate soak cannot start before the candidate commit")
    if (completed - started).total_seconds() < _MINIMUM_SOAK_SECONDS:
        _reject("release-candidate soak must span at least 30 complete days")
    if completed > current:
        _reject("release-candidate soak completion cannot be in the future")
    if type(soak.get("p0_p1_failures")) is not int or soak.get("p0_p1_failures") != 0:
        _reject("release-candidate soak must record zero P0/P1 failures")
    raw_projects = soak.get("external_projects")
    if not isinstance(raw_projects, list) or len(raw_projects) < 3:
        _reject("release-candidate soak requires at least three external projects")
    projects: set[str] = set()
    project_evidence: set[tuple[str, str]] = set()
    for index, raw_project in enumerate(raw_projects):
        project = _object(raw_project, f"soak.external_projects[{index}]")
        _exact_keys(
            project,
            {"name", "candidate_commit", "evidence"},
            f"soak.external_projects[{index}]",
        )
        name = _string(project, "name", f"soak.external_projects[{index}]")
        if project.get("candidate_commit") != candidate_commit:
            _reject(
                f"soak.external_projects[{index}].candidate_commit does not match the candidate"
            )
        if name in projects:
            _reject(f"duplicate external soak project: {name}")
        projects.add(name)
        evidence, artifact = _evidence_reference(
            root,
            project.get("evidence"),
            f"soak.external_projects[{index}].evidence",
            fetch_remote=fetch_remote,
        )
        if evidence in project_evidence:
            _reject(f"duplicate external soak evidence: {evidence}")
        project_evidence.add(evidence)
        _validate_soak_project(
            artifact,
            version=version,
            candidate_commit=candidate_commit,
            project=name,
            candidate_created=candidate_created,
            declared_started=started,
            declared_completed=completed,
            current=current,
            context=f"soak.external_projects[{index}].evidence artifact",
        )


def load_and_verify(
    path: Path,
    *,
    version: str,
    candidate_commit: str,
    candidate_created_epoch: int,
    root: Path,
) -> None:
    """Load and validate a release assurance JSON document."""
    try:
        document: object = json.loads(path.read_bytes(), object_pairs_hook=_unique_json_object)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, _DuplicateJSONKey) as exc:
        _reject(f"cannot read release assurance record: {type(exc).__name__}: {exc}")
    verify_assurance(
        _object(document, "release assurance"),
        version=version,
        candidate_commit=candidate_commit,
        candidate_created_epoch=candidate_created_epoch,
        root=root,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, default=Path("release/assurance.json"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-created-epoch", required=True, type=int)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        load_and_verify(
            arguments.record,
            version=arguments.version,
            candidate_commit=arguments.candidate_commit.lower(),
            candidate_created_epoch=arguments.candidate_created_epoch,
            root=arguments.repository.resolve(),
        )
    except ReleaseAssuranceError as exc:
        print(f"release assurance check failed: {exc}", file=sys.stderr)
        return 1
    print("release assurance check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
