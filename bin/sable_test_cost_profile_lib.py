#!/usr/bin/env python3
"""sable_test_cost_profile_lib — the shared machine-local test-cost profile
store and its publisher (SABLE-y4nom.7.2, contract v2.1 + round-1 review).

WHY THIS EXISTS. The cost reports that drive sable-dev-check's derived
budgets were written to repo-root-relative paths, so every git worktree had
its own (empty) copy and every plan ran under the flat fallback bound with
"no measured cost data". The store now lives in the GIT COMMON DIR — one
profile serves every checkout of the repository — and it is published, not
copied: ONE canonical profile.json embedding metadata and both cost maps,
atomically replaced, fingerprint-validated on every read.

THE READER IS FAIL-CLOSED ON SHAPE, NOT JUST PRESENCE. A missing store is
absent (flat bound, said plainly). Everything else — wrong top-level type,
non-map cost tables, non-finite or boolean seconds, identities outside the
stored catalog, an incomplete shell map, a provisional list that is not
exactly catalog-minus-measured, a stored digest that does not match the
stored arrays — is a LOUD refusal naming what is malformed. load_profile
returns (None, message); it never raises and never binds a doubtful byte.

THE CATALOG IS THE ACTUAL WORKING TREE. Python test modules are censused
from the filesystem (untracked additions count — the changed-path collector
sees them, so the catalog must too); the shell half is the sourced ALLOW
list. Discovery failure (broken shell-run-set.sh, failing git) REFUSES the
fingerprint rather than fingerprinting an empty catalog as data.

THE PUBLISHER RUNS THE PRODUCERS ITSELF, serially, under a nonblocking
kernel flock held from before the first producer through the atomic
replace; expected git/filesystem/spawn/serialization failures are
normalized to ProfilePublishError, the previous canonical file survives any
refusal byte-identical, and every staging path the attempt owns is removed
on the way out.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
PROFILE_NAME = "profile.json"
LOCK_NAME = "publish.lock"
# Reporter schema identities, part of the fingerprint: if either producer's
# output format changes, existing profiles must stop binding.
PYTHON_REPORT_SCHEMA = "conftest-cost-report-v1"
SHELL_PROFILE_SCHEMA = "shell-run-set-profile-tsv-v1"

_REQUIRED_PROFILE_KEYS = (
    "schema", "fingerprint", "python_costs", "shell_costs",
    "provisional_python", "provenance",
)


class ProfilePublishError(Exception):
    """A refusal anywhere in the publish protocol. The message names the
    exact condition; the canonical profile is never touched on refusal."""


def store_dir(repo_root: str | Path) -> Path:
    """<git-common-dir>/sable/test-cost — ONE store for every linked
    worktree of the repository (the same resolution the exact-object push
    gate uses)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise ProfilePublishError(f"git is unavailable: {exc}") from exc
    if result.returncode != 0:
        raise ProfilePublishError(
            f"cannot resolve the git common dir for {repo_root}: "
            f"{result.stderr.strip()}"
        )
    return Path(result.stdout.strip()) / "sable" / "test-cost"


def _valid_seconds(value) -> bool:
    """Finite, positive, and NOT bool (bool is an int subclass)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


# Tool-SPECIFIC version argvs (round-2 real-host discriminator): a generic
# --version records "Failed to parse global arguments" for dolt and
# "unknown option" for tmux — error strings, not identities — so upgrades of
# the two dominant integration tools would not re-fingerprint.
_TOOL_VERSION_ARGS = {
    "bd": ["--version"],
    "dolt": ["version"],
    "tmux": ["-V"],
    "git": ["--version"],
}


def _tool_identity(name: str) -> dict:
    """RESOLVED path (symlinks followed — the brew shim is not the tool) +
    the tool-specific version line. A PRESENT tool whose version probe fails
    or answers nothing REFUSES fingerprinting loudly — an error string is
    not an identity. Absence is a legitimate identity."""
    import shutil
    located = shutil.which(name)
    if not located:
        return {"path": None, "version": None}
    path = os.path.realpath(located)
    try:
        result = subprocess.run(
            [path, *_TOOL_VERSION_ARGS[name]],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfilePublishError(
            f"cannot fingerprint {name} at {path}: version probe failed "
            f"({exc})"
        ) from exc
    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0 or not output:
        raise ProfilePublishError(
            f"cannot fingerprint {name} at {path}: "
            f"{' '.join([path, *_TOOL_VERSION_ARGS[name]])} exited "
            f"{result.returncode} with {output[:80]!r} — an error string is "
            "not a version identity"
        )
    return {"path": path, "version": output.splitlines()[0].strip()}


def _pytest11_plugins() -> list:
    """Every installed pytest11 plugin as a collision-safe sorted list of
    [name, distribution, version] records. Enumeration failure REFUSES the
    fingerprint — a pseudo-plugin entry would be an ordinary bindable value
    hiding an unknown plugin set."""
    from importlib import metadata
    records = []
    try:
        entry_points = metadata.entry_points()
        group = (
            entry_points.select(group="pytest11")
            if hasattr(entry_points, "select")
            else entry_points.get("pytest11", [])
        )
        for ep in group:
            dist = getattr(ep, "dist", None)
            dist_name = getattr(dist, "name", None) if dist else None
            version = getattr(dist, "version", None) if dist else None
            records.append([ep.name, dist_name or "unknown", version or "unknown"])
    except Exception as exc:  # noqa: BLE001 — normalized to a refusal
        raise ProfilePublishError(
            f"cannot enumerate pytest11 plugins for the fingerprint: {exc}"
        ) from exc
    return sorted(records)


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _catalog(repo_root: Path) -> dict:
    """The ACTUAL test catalog: python test modules censused from the
    working tree (untracked additions included — the changed-path collector
    sees them, so the catalog must), and the sourced shell ALLOW list.
    Discovery failure refuses rather than returning empty arrays as data."""
    bin_dir = Path(repo_root) / "bin"
    python_tests = sorted(
        p.relative_to(repo_root).as_posix()
        for p in bin_dir.rglob("test_*.py")
    ) if bin_dir.is_dir() else []
    runset = Path(repo_root) / ".github/ci/shell-run-set.sh"
    if not runset.is_file():
        raise ProfilePublishError(
            f"catalog discovery failed: {runset} is absent"
        )
    try:
        allow = subprocess.run(
            ["bash", "-c",
             f"source {str(runset)!r} 2>/dev/null || exit 9; "
             "printf '%s\\n' \"${ALLOW[@]}\""],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise ProfilePublishError(
            f"catalog discovery failed: cannot spawn bash: {exc}"
        ) from exc
    shell_allow = sorted(l for l in allow.stdout.splitlines() if l)
    if allow.returncode != 0 or not shell_allow:
        raise ProfilePublishError(
            "catalog discovery failed: sourcing shell-run-set.sh exited "
            f"{allow.returncode} with {len(shell_allow)} ALLOW entr(ies) — an "
            "empty or unreadable ALLOW list is a refusal, not a catalog"
        )
    return {"python_tests": python_tests, "shell_allow": shell_allow}


def _catalog_digest(catalog: dict) -> str:
    return hashlib.sha256(
        json.dumps(catalog, sort_keys=True).encode()
    ).hexdigest()


def compute_fingerprint(repo_root: str | Path, **_kwargs) -> dict:
    """Everything that must be IDENTICAL between the measuring environment
    and the consuming one for the measurements to bind. Source SHA is
    deliberately absent — it is provenance (see publish), because per-commit
    invalidation would recreate the very cost this store removes. Raises
    ProfilePublishError when any identity cannot be established."""
    repo_root = Path(repo_root)
    catalog = _catalog(repo_root)
    return {
        "python_executable": sys.executable,
        "python_executable_resolved": os.path.realpath(sys.executable),
        "python_version": sys.version,
        "pytest11_plugins": _pytest11_plugins(),
        "tools": {name: _tool_identity(name)
                  for name in ("bd", "dolt", "tmux", "git")},
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cpu_model": _cpu_model(),
        "reporter_schemas": {
            "python": PYTHON_REPORT_SCHEMA,
            "shell": SHELL_PROFILE_SCHEMA,
        },
        "catalog": catalog,
        "catalog_digest": _catalog_digest(catalog),
    }


def _fingerprint_diff(stored: dict, current: dict) -> list[str]:
    """Human-readable field-level differences; catalog differences name the
    added/removed entries rather than just 'digest changed'."""
    diffs: list[str] = []
    keys = sorted(set(stored) | set(current))
    for key in keys:
        if key == "catalog":
            continue
        if stored.get(key) != current.get(key):
            if key == "catalog_digest":
                old_cat = stored.get("catalog") or {}
                new_cat = current.get("catalog") or {}
                named = False
                for half in ("python_tests", "shell_allow"):
                    old = set(old_cat.get(half, []) or [])
                    new = set(new_cat.get(half, []) or [])
                    added = sorted(new - old)
                    removed = sorted(old - new)
                    if added:
                        named = True
                        diffs.append(f"catalog {half} added: {' '.join(added)}")
                    if removed:
                        named = True
                        diffs.append(
                            f"catalog {half} removed: {' '.join(removed)}"
                        )
                if not named:
                    diffs.append("catalog_digest changed")
            else:
                diffs.append(
                    f"{key}: stored {stored.get(key)!r} != current "
                    f"{current.get(key)!r}"
                )
    return diffs


def _shape_error(profile) -> str | None:
    """Full canonical-shape validation (round-1 B1). Returns the loud
    malformation message, or None when every typed constraint holds."""
    if not isinstance(profile, dict):
        return f"top-level is {type(profile).__name__}, not an object"
    missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in profile]
    if missing:
        return f"missing section(s): {' '.join(missing)}"
    if profile.get("schema") != SCHEMA_VERSION:
        return f"schema {profile.get('schema')!r} != {SCHEMA_VERSION}"
    fingerprint = profile["fingerprint"]
    if not isinstance(fingerprint, dict):
        return "fingerprint is not an object"
    catalog = fingerprint.get("catalog")
    if not isinstance(catalog, dict):
        return "fingerprint.catalog is not an object"
    for half in ("python_tests", "shell_allow"):
        entries = catalog.get(half)
        if not isinstance(entries, list) or not all(
            isinstance(e, str) for e in entries
        ):
            return f"fingerprint.catalog.{half} is not a string array"
    stored_digest = fingerprint.get("catalog_digest")
    if stored_digest != _catalog_digest(
        {"python_tests": catalog["python_tests"],
         "shell_allow": catalog["shell_allow"]}
    ):
        return (
            "fingerprint.catalog_digest does not match the stored catalog "
            "arrays — the profile is self-inconsistent"
        )
    for name in ("python_costs", "shell_costs"):
        table = profile[name]
        if not isinstance(table, dict):
            return f"{name} is not a map"
        for identity, seconds in table.items():
            if not isinstance(identity, str):
                return f"{name} has a non-string identity: {identity!r}"
            if not _valid_seconds(seconds):
                return (
                    f"{name}[{identity}] is not a finite positive "
                    f"non-boolean number: {seconds!r}"
                )
    provisional = profile["provisional_python"]
    if not isinstance(provisional, list) or not all(
        isinstance(e, str) for e in provisional
    ):
        return "provisional_python is not a string array"
    provenance = profile["provenance"]
    if not isinstance(provenance, dict):
        return "provenance is not an object"
    source_sha = provenance.get("source_sha")
    if not isinstance(source_sha, str) or not source_sha:
        return "provenance.source_sha is not a non-empty string"
    argvs = provenance.get("producer_argvs")
    # EXACTLY two non-empty argv arrays (python then shell) — an empty list
    # satisfies an all(...) vacuously and would bind (round-1 live check).
    if not isinstance(argvs, list) or len(argvs) != 2 or not all(
        isinstance(argv, list) and argv
        and all(isinstance(arg, str) for arg in argv)
        for argv in argvs
    ):
        return (
            "provenance.producer_argvs is not exactly two non-empty string "
            "arrays"
        )
    wall = provenance.get("wall_seconds")
    if not isinstance(wall, dict) or set(wall) != {"python", "shell"} or not all(
        isinstance(v, (int, float))
        and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        for v in wall.values()
    ):
        return (
            "provenance.wall_seconds is not exactly {python, shell} finite "
            "durations"
        )
    timestamp = provenance.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return "provenance.timestamp is not a non-empty string"
    doctor = provenance.get("doctor")
    if not isinstance(doctor, dict) or not isinstance(doctor.get("status"), str)             or not doctor.get("status"):
        return "provenance.doctor has no typed non-empty status"
    python_catalog = set(catalog["python_tests"])
    measured = set(profile["python_costs"])
    if not measured <= python_catalog:
        extras = sorted(measured - python_catalog)
        return f"python_costs identities outside the catalog: {' '.join(extras)}"
    if sorted(provisional) != sorted(python_catalog - measured):
        return (
            "provisional_python is not exactly catalog-minus-measured "
            f"(stored {sorted(provisional)!r})"
        )
    shell_catalog = set(catalog["shell_allow"])
    shell_measured = set(profile["shell_costs"])
    if shell_measured != shell_catalog:
        missing_suites = sorted(shell_catalog - shell_measured)
        extras = sorted(shell_measured - shell_catalog)
        return (
            "shell_costs is not exactly the stored ALLOW catalog"
            + (f"; missing: {' '.join(missing_suites)}" if missing_suites else "")
            + (f"; extras: {' '.join(extras)}" if extras else "")
        )
    return None


def load_profile(repo_root: str | Path) -> tuple[dict | None, str | None]:
    """(costs, message). costs is {"python": {...}, "shell": {...},
    "provisional_python": [...]} when the store binds; otherwise None with a
    message — quiet for plain absence, LOUD (detailed) for any
    malformation or fingerprint/catalog mismatch. Never raises."""
    try:
        path = store_dir(repo_root) / PROFILE_NAME
    except ProfilePublishError as exc:
        return None, str(exc)
    if not path.is_file():
        return None, "no shared cost profile has been published on this machine"
    try:
        profile = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, f"shared cost profile {path} is unreadable: {exc}"
    shape = _shape_error(profile)
    if shape:
        return None, (
            f"shared cost profile {path} is malformed — {shape}; refusing to "
            "bind, republish with sable-dev-check --publish-cost-profile"
        )
    try:
        current = compute_fingerprint(repo_root)
    except ProfilePublishError as exc:
        return None, str(exc)
    diffs = _fingerprint_diff(profile["fingerprint"], current)
    if diffs:
        return None, (
            "shared cost profile fingerprint mismatch — measurements from a "
            "different environment/catalog never bind: "
            + "; ".join(diffs)
            + ". Republish with sable-dev-check --publish-cost-profile."
        )
    return {
        "python": profile["python_costs"],
        "shell": profile["shell_costs"],
        "provisional_python": profile["provisional_python"],
    }, None


def _producer_argvs(repo_root: Path, staging_py: Path, staging_tsv: Path):
    """The exact producer commands, recorded verbatim in provenance so
    byte-equivalence for a future baseline claim is mechanical (v2.1 C4)."""
    # -rs + --sable-report-skip-set: the publisher's python producer is an
    # AUTHORITATIVE full run, and every documented/authoritative full run
    # enables skip-identity reporting (test_conftest.py pins the doctrine —
    # discovered the measured way, when the first real publish failed on the
    # doc-sync assertion). Skip state lives under gitignored
    # .claude/sable/state/, so the porcelain recheck stays clean.
    return [
        [sys.executable, "-m", "pytest", "bin/", "-q", "-rs", "-p",
         "no:cacheprovider", "--sable-report-skip-set",
         f"--sable-test-cost-report={staging_py}"],
        ["bash", ".github/ci/shell-run-set.sh", "--profile", str(staging_tsv)],
    ]


def _git_state(repo_root: Path) -> tuple[str, str]:
    """HEAD + porcelain, fail-closed: a git that errors or cannot spawn is a
    refusal — unverifiable source state never reads as clean/unchanged."""
    try:
        head_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        porcelain_result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise ProfilePublishError(f"git is unavailable: {exc}") from exc
    if head_result.returncode != 0 or porcelain_result.returncode != 0:
        raise ProfilePublishError(
            "cannot establish the source state: git exited "
            f"{head_result.returncode}/{porcelain_result.returncode} "
            f"({(head_result.stderr or porcelain_result.stderr).strip()})"
        )
    return head_result.stdout.strip(), porcelain_result.stdout


def _doctor_record(repo_root: Path) -> dict:
    doctor = Path(repo_root) / "bin" / "sable-doctor"
    if not doctor.is_file():
        return {"status": "unavailable", "detail": "bin/sable-doctor absent"}
    try:
        result = subprocess.run(
            [str(doctor)], capture_output=True, text=True, timeout=120,
            cwd=repo_root,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {
            "status": "rc0" if result.returncode == 0 else f"rc{result.returncode}",
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "first_line": output.splitlines()[0] if output.splitlines() else "",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "detail": str(exc)}


def _validate_python_report(raw_path: Path, catalog: dict):
    """Strict against conftest.build_cost_report's ACTUAL schema:
    {thresholds: object, tests: array, modules: array, violations: array},
    violations empty, finite positive non-boolean seconds, identities inside
    the tracked catalog, no duplicates."""
    try:
        report = json.loads(raw_path.read_text())
    except (OSError, ValueError) as exc:
        raise ProfilePublishError(
            f"python producer output is unreadable: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise ProfilePublishError(
            "python producer output is not an object (schema violation)"
        )
    thresholds = report.get("thresholds")
    if not isinstance(thresholds, dict) or not all(
        _valid_seconds(thresholds.get(key))
        for key in ("ordinary_test_seconds", "ordinary_module_seconds")
    ):
        raise ProfilePublishError(
            "python producer output has no valid thresholds object "
            "(ordinary_test_seconds/ordinary_module_seconds must be finite "
            "positive numbers — schema violation)"
        )
    tests = report.get("tests")
    if not isinstance(tests, list) or not all(
        isinstance(row, dict)
        and isinstance(row.get("nodeid"), str)
        and isinstance(row.get("module"), str)
        and _valid_seconds(row.get("seconds"))
        for row in tests
    ):
        raise ProfilePublishError(
            "python producer output has no valid tests list (each row needs "
            "nodeid/module strings and finite positive seconds — schema "
            "violation)"
        )
    modules = report.get("modules")
    if not isinstance(modules, list):
        raise ProfilePublishError(
            "python producer output has no modules list (schema violation)"
        )
    violations = report.get("violations")
    if not isinstance(violations, list):
        raise ProfilePublishError(
            "python producer output has no violations list (schema violation)"
        )
    if violations:
        raise ProfilePublishError(
            "python producer reported load-boundary violation(s): "
            f"{violations!r} — fix them before publishing"
        )
    known = set(catalog["python_tests"])
    costs: dict[str, float] = {}
    for record in modules:
        if not isinstance(record, dict) or not record.get("module"):
            raise ProfilePublishError(
                f"invalid python module record: {record!r}"
            )
        identity = str(record["module"])
        if identity in costs:
            raise ProfilePublishError(
                f"duplicate python module identity: {identity}"
            )
        if identity not in known:
            raise ProfilePublishError(
                f"unexpected python module identity (not in the catalog): "
                f"{identity}"
            )
        seconds = record.get("seconds")
        if not _valid_seconds(seconds):
            raise ProfilePublishError(
                f"invalid seconds for {identity}: {seconds!r} (must be a "
                "finite positive non-boolean number)"
            )
        costs[identity] = float(seconds)
    provisional = sorted(known - set(costs))
    return costs, provisional


def _validate_shell_profile(raw_path: Path, catalog: dict) -> dict:
    try:
        lines = raw_path.read_text().splitlines()
    except OSError as exc:
        raise ProfilePublishError(
            f"shell producer output is unreadable: {exc}"
        ) from exc
    if not lines or lines[0] != "suite\tstatus\tseconds":
        raise ProfilePublishError(
            f"shell producer output has an invalid header: "
            f"{lines[0] if lines else '<empty>'!r}"
        )
    allow = set(catalog["shell_allow"])
    costs: dict[str, float] = {}
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise ProfilePublishError(f"invalid shell profile row: {line!r}")
        identity, status, raw_seconds = fields
        if identity in costs:
            raise ProfilePublishError(
                f"duplicate shell suite identity: {identity}"
            )
        if identity not in allow:
            raise ProfilePublishError(
                f"unexpected shell suite identity (not in ALLOW): {identity}"
            )
        if status != "pass":
            raise ProfilePublishError(
                f"shell suite {identity} did not pass (status {status!r}); "
                "a profile is only published from an all-green serial run"
            )
        try:
            seconds = float(raw_seconds)
        except ValueError as exc:
            raise ProfilePublishError(
                f"invalid seconds for {identity}: {raw_seconds!r}"
            ) from exc
        if not _valid_seconds(seconds):
            raise ProfilePublishError(
                f"invalid seconds for {identity}: {seconds!r} (must be a "
                "finite positive number)"
            )
        costs[identity] = seconds
    missing = sorted(allow - set(costs))
    if missing:
        raise ProfilePublishError(
            "shell producer output is missing ALLOW suite(s): "
            + " ".join(missing)
        )
    return costs


def publish(
    repo_root: str | Path,
    *,
    runner=subprocess.run,
    clock=time.time,
) -> dict:
    """Run both producers serially under the store flock, validate
    everything, and atomically replace the canonical profile. Every
    expected failure — git, filesystem, producer spawn, serialization — is
    normalized to ProfilePublishError; the existing canonical file survives
    any refusal byte-identical, and every staging path this attempt owns is
    removed on the way out."""
    repo_root = Path(repo_root)
    store = store_dir(repo_root)
    try:
        store.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(store / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise ProfilePublishError(
            f"cannot prepare the store at {store}: {exc}"
        ) from exc
    try:
        os.set_inheritable(lock_fd, False)
    except OSError as exc:
        os.close(lock_fd)
        raise ProfilePublishError(
            f"cannot mark the lock fd non-inheritable: {exc}"
        ) from exc
    staging_py = store / f".staging-{os.getpid()}-python.json"
    staging_tsv = store / f".staging-{os.getpid()}-shell.tsv"
    staging_canonical = store / f".staging-{os.getpid()}-profile.json"
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProfilePublishError(
                "another publisher holds the cost-profile lock — a second "
                "expensive serial run is refused, not queued"
            ) from exc

        head_before, porcelain_before = _git_state(repo_root)
        if porcelain_before.strip():
            raise ProfilePublishError(
                "the producing worktree must be clean (git status --porcelain "
                "is non-empty); commit or stash before publishing"
            )
        fingerprint_before = compute_fingerprint(repo_root)

        try:
            for stale in (staging_py, staging_tsv, staging_canonical):
                stale.unlink(missing_ok=True)
        except OSError as exc:
            raise ProfilePublishError(
                f"cannot clear stale staging files: {exc}"
            ) from exc

        argvs = _producer_argvs(repo_root, staging_py, staging_tsv)
        wall: dict[str, float] = {}
        for kind, argv in zip(("python", "shell"), argvs):
            started = time.monotonic()
            try:
                result = runner(argv, cwd=repo_root)
            except OSError as exc:
                raise ProfilePublishError(
                    f"{kind} producer could not be spawned: {exc}"
                ) from exc
            wall[kind] = time.monotonic() - started
            rc = getattr(result, "returncode", 0)
            if rc != 0:
                raise ProfilePublishError(
                    f"{kind} producer exited {rc}; a profile is only "
                    "published from a fully green serial run"
                )

        catalog = fingerprint_before["catalog"]
        python_costs, provisional = _validate_python_report(
            staging_py, catalog,
        )
        shell_costs = _validate_shell_profile(staging_tsv, catalog)

        head_after, porcelain_after = _git_state(repo_root)
        if head_after != head_before:
            raise ProfilePublishError(
                f"HEAD moved during the producer runs "
                f"({head_before[:12]} -> {head_after[:12]}); the "
                "measurements describe no single source state — refused"
            )
        if porcelain_after.strip():
            raise ProfilePublishError(
                "the worktree stopped being clean during the producer "
                "runs (git status --porcelain is non-empty) — refused"
            )
        fingerprint_after = compute_fingerprint(repo_root)
        if fingerprint_before != fingerprint_after:
            raise ProfilePublishError(
                "the environment/catalog fingerprint changed during the "
                "producer runs: "
                + "; ".join(
                    _fingerprint_diff(fingerprint_before, fingerprint_after)
                )
                + " — refused"
            )

        profile = {
            "schema": SCHEMA_VERSION,
            "fingerprint": fingerprint_before,
            "python_costs": python_costs,
            "shell_costs": shell_costs,
            "provisional_python": provisional,
            "provenance": {
                "source_sha": head_before,
                "producer_argvs": [[str(a) for a in argv] for argv in argvs],
                "wall_seconds": wall,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(clock()),
                ),
                "doctor": _doctor_record(repo_root),
            },
        }

        try:
            with open(staging_canonical, "w") as handle:
                # allow_nan=False: the final serialization guard — a NaN that
                # slipped every validation still cannot reach the store.
                json.dump(
                    profile, handle, indent=2, sort_keys=True, allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging_canonical, store / PROFILE_NAME)
            dir_fd = os.open(str(store), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, ValueError, TypeError) as exc:
            # TypeError: an unserializable object that reached the profile
            # dict (e.g. via a doctor record) is a serialization failure
            # like any other — normalized, old canonical intact.
            raise ProfilePublishError(
                f"cannot write the canonical profile: {exc}"
            ) from exc
        return profile
    finally:
        for staging in (staging_py, staging_tsv, staging_canonical):
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        os.close(lock_fd)
