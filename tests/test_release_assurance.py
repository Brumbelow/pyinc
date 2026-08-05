from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.check_release_assurance import ReleaseAssuranceError, verify_assurance
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_release_assurance import (  # noqa: E402
        ReleaseAssuranceError,
        verify_assurance,
    )

VERSION = "3.1.2"
COMMIT = "a" * 40
NOW = datetime(2026, 8, 4, tzinfo=UTC)
CANDIDATE_EPOCH = int(datetime(2026, 5, 31, tzinfo=UTC).timestamp())


def _artifact_bytes(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _evidence_payloads() -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for discipline, reviewer, prefix in (
        ("incremental-computation", "Reviewer One", "incremental"),
        ("filesystem-security", "Reviewer Two", "filesystem"),
    ):
        payloads[f"https://example.test/{prefix}-scope"] = _artifact_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "review-scope",
                "version": VERSION,
                "candidate_commit": COMMIT,
                "discipline": discipline,
                "reviewer": reviewer,
                "recorded_utc": "2026-06-02T00:00:00Z",
                "method": "Independent adversarial source and runtime review",
                "scope": ["runtime soundness", "checkpoint persistence"],
            }
        )
        payloads[f"https://example.test/{prefix}-report"] = _artifact_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "review-report",
                "version": VERSION,
                "candidate_commit": COMMIT,
                "discipline": discipline,
                "reviewer": reviewer,
                "completed_utc": "2026-06-15T00:00:00Z",
                "result": "pass",
                "open_p0_p1": 0,
                "summary": "No open release-blocking correctness findings.",
                "closure_evidence": ["named regression corpus passed"],
            }
        )
        payloads[f"https://example.test/{prefix}-corpus"] = _artifact_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "counterexample-corpus",
                "version": VERSION,
                "candidate_commit": COMMIT,
                "discipline": discipline,
                "reviewer": reviewer,
                "recorded_utc": "2026-06-10T00:00:00Z",
                "cases": [f"{prefix}-counterexample-1"],
            }
        )
    for name, suffix in (
        ("project-one", "one"),
        ("project-two", "two"),
        ("project-three", "three"),
    ):
        payloads[f"https://example.test/{suffix}"] = _artifact_bytes(
            {
                "schema_version": 1,
                "evidence_kind": "soak-project",
                "version": VERSION,
                "candidate_commit": COMMIT,
                "project": name,
                "started_utc": "2026-06-01T00:00:00Z",
                "completed_utc": "2026-07-01T00:00:00Z",
                "result": "pass",
                "p0_p1_failures": 0,
                "runs": 30,
                "environment": "External production-like integration",
                "summary": "Daily validation completed without P0/P1 failures.",
            }
        )
    return payloads


EVIDENCE_PAYLOADS = _evidence_payloads()


def _ready_record() -> dict[str, object]:
    def evidence(location: str) -> dict[str, str]:
        return {
            "location": location,
            "sha256": hashlib.sha256(EVIDENCE_PAYLOADS[location]).hexdigest(),
        }

    return {
        "schema_version": 3,
        "version": VERSION,
        "candidate_commit": COMMIT,
        "status": "ready",
        "open_p0_p1": 0,
        "reviews": [
            {
                "discipline": "incremental-computation",
                "candidate_commit": COMMIT,
                "reviewer": "Reviewer One",
                "scope": evidence("https://example.test/incremental-scope"),
                "report": evidence("https://example.test/incremental-report"),
                "counterexample_corpus": evidence("https://example.test/incremental-corpus"),
            },
            {
                "discipline": "filesystem-security",
                "candidate_commit": COMMIT,
                "reviewer": "Reviewer Two",
                "scope": evidence("https://example.test/filesystem-scope"),
                "report": evidence("https://example.test/filesystem-report"),
                "counterexample_corpus": evidence("https://example.test/filesystem-corpus"),
            },
        ],
        "soak": {
            "candidate_commit": COMMIT,
            "started_utc": "2026-06-01T00:00:00Z",
            "completed_utc": "2026-07-01T00:00:00Z",
            "p0_p1_failures": 0,
            "external_projects": [
                {
                    "name": "project-one",
                    "candidate_commit": COMMIT,
                    "evidence": evidence("https://example.test/one"),
                },
                {
                    "name": "project-two",
                    "candidate_commit": COMMIT,
                    "evidence": evidence("https://example.test/two"),
                },
                {
                    "name": "project-three",
                    "candidate_commit": COMMIT,
                    "evidence": evidence("https://example.test/three"),
                },
            ],
        },
    }


def _verify(
    record: dict[str, object],
    root: Path,
    *,
    now_utc: datetime = NOW,
    candidate_created_epoch: int = CANDIDATE_EPOCH,
    evidence_payloads: Mapping[str, bytes] = EVIDENCE_PAYLOADS,
) -> None:
    verify_assurance(
        record,
        version=VERSION,
        candidate_commit=COMMIT,
        candidate_created_epoch=candidate_created_epoch,
        root=root,
        now_utc=now_utc,
        fetch_remote=evidence_payloads.__getitem__,
    )


def test_accepts_exact_independent_reviews_and_thirty_day_soak(tmp_path: Path) -> None:
    _verify(_ready_record(), tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("status", "blocked"), "not ready"),
        (("candidate_commit", "b" * 40), "candidate_commit"),
        (("open_p0_p1", 1), "zero open P0/P1"),
        (("reviews", []), "requires incremental-computation"),
    ],
)
def test_rejects_incomplete_candidate_or_reviews(
    tmp_path: Path, mutation: tuple[str, object], message: str
) -> None:
    record = _ready_record()
    record[mutation[0]] = mutation[1]
    with pytest.raises(ReleaseAssuranceError, match=message):
        _verify(record, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("completed_utc", "2026-06-30T23:59:59Z", "30 complete days"),
        ("p0_p1_failures", 1, "zero P0/P1 failures"),
        ("external_projects", [], "at least three external projects"),
    ],
)
def test_rejects_short_or_failed_soak(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    record = _ready_record()
    soak = deepcopy(record["soak"])
    assert isinstance(soak, dict)
    soak[field] = value
    record["soak"] = soak
    with pytest.raises(ReleaseAssuranceError, match=message):
        _verify(record, tmp_path)


def test_rejects_boolean_counts_duplicate_reviewers_and_future_soak(tmp_path: Path) -> None:
    boolean_count = _ready_record()
    boolean_count["open_p0_p1"] = False
    with pytest.raises(ReleaseAssuranceError, match="zero open P0/P1"):
        _verify(boolean_count, tmp_path)

    duplicate_reviewer = _ready_record()
    reviews = deepcopy(duplicate_reviewer["reviews"])
    assert isinstance(reviews, list)
    assert isinstance(reviews[1], dict)
    reviews[1]["reviewer"] = "Reviewer One"
    duplicate_reviewer["reviews"] = reviews
    with pytest.raises(ReleaseAssuranceError, match="distinct reviewers"):
        _verify(duplicate_reviewer, tmp_path)

    future_soak = _ready_record()
    soak = deepcopy(future_soak["soak"])
    assert isinstance(soak, dict)
    soak["started_utc"] = "2026-07-05T00:00:00Z"
    soak["completed_utc"] = "2026-08-05T00:00:00Z"
    future_soak["soak"] = soak
    with pytest.raises(ReleaseAssuranceError, match="cannot be in the future"):
        _verify(future_soak, tmp_path)


def test_repository_evidence_must_stay_inside_the_candidate(tmp_path: Path) -> None:
    record = _ready_record()
    reviews = deepcopy(record["reviews"])
    assert isinstance(reviews, list)
    assert isinstance(reviews[0], dict)
    reviews[0]["report"] = {"location": "../outside.md", "sha256": "0" * 64}
    record["reviews"] = reviews

    with pytest.raises(ReleaseAssuranceError, match="stay inside"):
        _verify(record, tmp_path)


def test_evidence_content_must_match_its_bound_digest(tmp_path: Path) -> None:
    record = _ready_record()
    reviews = deepcopy(record["reviews"])
    assert isinstance(reviews, list)
    assert isinstance(reviews[0], dict)
    report = deepcopy(reviews[0]["report"])
    assert isinstance(report, dict)
    report["sha256"] = "0" * 64
    reviews[0]["report"] = report
    record["reviews"] = reviews

    with pytest.raises(ReleaseAssuranceError, match="SHA-256 does not match"):
        _verify(record, tmp_path)


def _replace_review_artifact(
    record: dict[str, object],
    field: str,
    artifact: Mapping[str, object] | bytes,
) -> dict[str, bytes]:
    reviews = record["reviews"]
    assert isinstance(reviews, list)
    review = reviews[0]
    assert isinstance(review, dict)
    reference = review[field]
    assert isinstance(reference, dict)
    location = reference["location"]
    assert isinstance(location, str)
    payload = artifact if isinstance(artifact, bytes) else _artifact_bytes(artifact)
    reference["sha256"] = hashlib.sha256(payload).hexdigest()
    payloads = dict(EVIDENCE_PAYLOADS)
    payloads[location] = payload
    return payloads


def _replace_project_artifact(
    record: dict[str, object], artifact: Mapping[str, object]
) -> dict[str, bytes]:
    soak = record["soak"]
    assert isinstance(soak, dict)
    projects = soak["external_projects"]
    assert isinstance(projects, list)
    project = projects[0]
    assert isinstance(project, dict)
    reference = project["evidence"]
    assert isinstance(reference, dict)
    location = reference["location"]
    assert isinstance(location, str)
    payload = _artifact_bytes(artifact)
    reference["sha256"] = hashlib.sha256(payload).hexdigest()
    payloads = dict(EVIDENCE_PAYLOADS)
    payloads[location] = payload
    return payloads


def test_unrelated_hash_bound_document_cannot_satisfy_review_evidence(
    tmp_path: Path,
) -> None:
    record = _ready_record()
    payloads = _replace_review_artifact(
        record,
        "report",
        b"# Release notes\n\nThis is correctly hashed but is not review evidence.\n",
    )

    with pytest.raises(ReleaseAssuranceError, match="UTF-8 JSON artifact"):
        _verify(record, tmp_path, evidence_payloads=payloads)


def test_evidence_rejects_ambiguous_duplicate_json_keys(tmp_path: Path) -> None:
    record = _ready_record()
    payload = EVIDENCE_PAYLOADS["https://example.test/incremental-report"].replace(
        b'"result":"pass"', b'"result":"failed","result":"pass"'
    )
    payloads = _replace_review_artifact(record, "report", payload)

    with pytest.raises(ReleaseAssuranceError, match="duplicate JSON key 'result'"):
        _verify(record, tmp_path, evidence_payloads=payloads)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence_kind", "review-scope", "evidence_kind"),
        ("candidate_commit", "b" * 40, "candidate_commit"),
        ("reviewer", "Someone Else", "reviewer"),
        ("result", "incomplete", "result must be 'pass'"),
        ("open_p0_p1", 1, "zero open P0/P1 findings"),
        ("completed_utc", "2026-05-30T00:00:00Z", "cannot predate"),
    ],
)
def test_review_report_artifact_is_semantically_bound(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    record = _ready_record()
    artifact = json.loads(EVIDENCE_PAYLOADS["https://example.test/incremental-report"])
    assert isinstance(artifact, dict)
    artifact[field] = value
    payloads = _replace_review_artifact(record, "report", artifact)

    with pytest.raises(ReleaseAssuranceError, match=message):
        _verify(record, tmp_path, evidence_payloads=payloads)


def test_review_artifact_schema_is_exact_and_chronological(tmp_path: Path) -> None:
    record = _ready_record()
    scope = json.loads(EVIDENCE_PAYLOADS["https://example.test/incremental-scope"])
    assert isinstance(scope, dict)
    scope["unvalidated_assertion"] = True
    payloads = _replace_review_artifact(record, "scope", scope)
    with pytest.raises(ReleaseAssuranceError, match="unexpected unvalidated_assertion"):
        _verify(record, tmp_path, evidence_payloads=payloads)

    record = _ready_record()
    corpus = json.loads(EVIDENCE_PAYLOADS["https://example.test/incremental-corpus"])
    assert isinstance(corpus, dict)
    corpus["recorded_utc"] = "2026-06-16T00:00:00Z"
    payloads = _replace_review_artifact(record, "counterexample_corpus", corpus)
    with pytest.raises(ReleaseAssuranceError, match="corpus cannot postdate"):
        _verify(record, tmp_path, evidence_payloads=payloads)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project", "different-project", "project does not match"),
        ("candidate_commit", "b" * 40, "candidate_commit"),
        ("result", "failed", "result must be 'pass'"),
        ("p0_p1_failures", 1, "zero P0/P1 failures"),
        ("runs", 0, "positive integer"),
        ("started_utc", "2026-06-02T00:00:00Z", "30 complete days"),
    ],
)
def test_soak_project_artifact_is_semantically_bound(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    record = _ready_record()
    artifact = json.loads(EVIDENCE_PAYLOADS["https://example.test/one"])
    assert isinstance(artifact, dict)
    artifact[field] = value
    payloads = _replace_project_artifact(record, artifact)

    with pytest.raises(ReleaseAssuranceError, match=message):
        _verify(record, tmp_path, evidence_payloads=payloads)


def test_every_review_and_soak_record_is_bound_to_the_candidate(tmp_path: Path) -> None:
    record = _ready_record()
    reviews = deepcopy(record["reviews"])
    assert isinstance(reviews, list)
    assert isinstance(reviews[0], dict)
    reviews[0]["candidate_commit"] = "b" * 40
    record["reviews"] = reviews
    with pytest.raises(ReleaseAssuranceError, match="review incremental-computation"):
        _verify(record, tmp_path)

    record = _ready_record()
    soak = deepcopy(record["soak"])
    assert isinstance(soak, dict)
    projects = soak["external_projects"]
    assert isinstance(projects, list)
    assert isinstance(projects[0], dict)
    projects[0]["candidate_commit"] = "b" * 40
    record["soak"] = soak
    with pytest.raises(ReleaseAssuranceError, match=r"external_projects\[0\]"):
        _verify(record, tmp_path)


def test_soak_cannot_predate_the_exact_candidate(tmp_path: Path) -> None:
    after_soak_started = int(datetime(2026, 6, 2, tzinfo=UTC).timestamp())

    with pytest.raises(ReleaseAssuranceError, match="cannot start before"):
        _verify(_ready_record(), tmp_path, candidate_created_epoch=after_soak_started)
