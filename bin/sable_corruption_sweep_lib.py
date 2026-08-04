#!/usr/bin/env python3
"""Rank stored bead and memory text for shell-substitution corruption.

The detector is intentionally read-only and heuristic.  A match is a triage
candidate, never permission to edit a record.  Strong signatures identify
structured command output embedded in prose; weak signatures identify scars
left when command substitution produced no stdout.  The latter cannot prove
corruption and are labelled accordingly.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Signature:
    name: str
    weight: int
    confidence: str
    pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class Finding:
    source_type: str
    source_id: str
    field: str
    signature: str
    confidence: str
    weight: int
    evidence: str


@dataclass(frozen=True)
class Candidate:
    source_type: str
    source_id: str
    score: int
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["findings"] = [asdict(finding) for finding in self.findings]
        return value


SIGNATURES: tuple[Signature, ...] = (
    Signature(
        "bd_json_record",
        8,
        "strong",
        re.compile(
            r'(?s)(?:"created_at"\s*:.*"dependency_count"\s*:|'
            r'"dependency_type"\s*:.*"dependent_count"\s*:|'
            r'"dependent_count"\s*:.*"dependency_count"\s*:)',
        ),
    ),
    Signature(
        "bd_listing",
        7,
        "strong",
        re.compile(
            r"(?m)(?:^[○◐✓]\s+SABLE-|^●\s+P[0-4]\b|Status:\s*[○◐✓]\s+open\b|"
            r"Total:\s*\d+\s+issues\b|Showing\s+\d+\s+of\s+\d+\s+ready issues)",
        ),
    ),
    Signature(
        "bd_stderr",
        6,
        "strong",
        re.compile(r"(?m)(?:No updates specified|(?:bd|beads): .*not found)"),
    ),
    Signature(
        "git_output",
        5,
        "strong",
        re.compile(
            r"(?m)(?:^[0-9a-f]{40}$|^\*\s+branch\b|->\s+FETCH_HEAD\s*$)"
        ),
    ),
    Signature(
        "command_echo",
        4,
        "medium",
        re.compile(
            r"(?m)^\s*(?:\$\s*)?bd\s+(?:show|list|ready|update|create|close|memories|remember)\b"
        ),
    ),
    Signature(
        "empty_substitution_scar",
        2,
        "weak",
        re.compile(
            r"(?m)(?:\b(?:and|or)\s+[.,]|[:,]\s{2,}(?:must|should|will|is|are)\b)"
        ),
    ),
)

DETECTION_BOUNDARIES = (
    "Strong matches detect embedded bd/git output, not author intent; records that quote tooling may be legitimate.",
    "Weak empty-substitution scars are triage hints with expected false positives.",
    "Fluent prose produced by a successful substitution can leave no mechanical scar and is not detectable here.",
    "The sweep never edits, closes, or repairs a bead or memory.",
)


def signature_names() -> frozenset[str]:
    return frozenset(signature.name for signature in SIGNATURES) | {"size_anomaly"}


def _evidence(text: str, start: int, end: int, limit: int = 180) -> str:
    left = max(0, start - 50)
    right = min(len(text), end + 80)
    return " ".join(text[left:right].split())[:limit]


def scan_text(
    source_type: str,
    source_id: str,
    field: str,
    text: str,
    *,
    enabled: frozenset[str] | None = None,
) -> list[Finding]:
    if not isinstance(text, str) or not text:
        return []
    active = signature_names() if enabled is None else enabled
    findings: list[Finding] = []
    for signature in SIGNATURES:
        if signature.name not in active or signature.pattern is None:
            continue
        match = signature.pattern.search(text)
        if match:
            findings.append(
                Finding(
                    source_type,
                    source_id,
                    field,
                    signature.name,
                    signature.confidence,
                    signature.weight,
                    _evidence(text, match.start(), match.end()),
                )
            )
    if "size_anomaly" in active and len(text) >= 50_000:
        findings.append(
            Finding(
                source_type,
                source_id,
                field,
                "size_anomaly",
                "weak",
                1,
                f"field length is {len(text)} characters (threshold 50000)",
            )
        )
    return findings


def scan_corpus(
    issues: Sequence[Mapping[str, object]],
    memories: Mapping[str, object],
    *,
    enabled: frozenset[str] | None = None,
) -> list[Candidate]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for issue in issues:
        issue_id = str(issue.get("id") or "<missing-id>")
        for field in ("description", "notes"):
            findings = scan_text(
                "bead", issue_id, field, issue.get(field) or "", enabled=enabled
            )
            grouped.setdefault(("bead", issue_id), []).extend(findings)
    for key, body in memories.items():
        findings = scan_text(
            "memory", str(key), "body", body or "", enabled=enabled
        )
        grouped.setdefault(("memory", str(key)), []).extend(findings)

    candidates = [
        Candidate(kind, identifier, sum(item.weight for item in findings), tuple(findings))
        for (kind, identifier), findings in grouped.items()
        if findings
    ]
    return sorted(candidates, key=lambda item: (-item.score, item.source_type, item.source_id))


def report(
    issues: Sequence[Mapping[str, object]],
    memories: Mapping[str, object],
    *,
    enabled: frozenset[str] | None = None,
) -> dict:
    candidates = scan_corpus(issues, memories, enabled=enabled)
    return {
        "schema": 1,
        "read_only": True,
        "scanned": {"beads": len(issues), "memories": len(memories)},
        "enabled_signatures": sorted(signature_names() if enabled is None else enabled),
        "detection_boundaries": list(DETECTION_BOUNDARIES),
        "candidate_count": len(candidates),
        "candidates": [candidate.to_dict() for candidate in candidates],
    }
