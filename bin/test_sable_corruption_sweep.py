"""Unit contracts for the read-only tmbx1 corpus detector (SABLE-wf7rx)."""
from sable_corruption_sweep_lib import report, scan_text, signature_names


def signatures(text):
    return {finding.signature for finding in scan_text("bead", "x", "notes", text)}


def test_clean_long_description_scores_clean():
    clean = ("A deliberately long but ordinary design explanation. " * 600).strip()
    assert len(clean) > 20_000
    assert scan_text("bead", "clean", "description", clean) == []


def test_each_structured_signature_class_is_flagged():
    assert "bd_json_record" in signatures(
        'prefix {"created_at":"now","dependency_count":2} suffix'
    )
    assert "bd_stderr" in signatures("the write returned No updates specified here")
    assert "bd_listing" in signatures("prose before\n○ SABLE-ab12 ● P1 [bug] injected\nprose")
    assert "git_output" in signatures("output\n0123456789abcdef0123456789abcdef01234567\nend")
    assert "command_echo" in signatures("bd ready --limit 0\n")
    assert "empty_substitution_scar" in signatures("PROPOSED:  must report both axes")


def test_egm0z_preserved_live_specimen_shape_is_a_known_positive():
    # Captured from SABLE-egm0z's deliberately preserved warning block: a
    # ready-pool row was spliced into authored prose by a backticked bd ready.
    specimen = (
        "FIX SHAPE — separate recording from work. It cannot enter "
        "○ SABLE-cmar4 ● P0 [epic] Standardized CI-tier ladder\n"
        "○ SABLE-jd5fj ● P0 [epic] Merge pipeline v2"
    )
    assert "bd_listing" in signatures(specimen)


def test_report_covers_bead_descriptions_notes_and_memories_and_ranks():
    result = report(
        [{"id": "SABLE-one", "description": "clean", "notes": "No updates specified"}],
        {"fleet-rule": "broken and . A clause vanished"},
    )
    identities = {(item["source_type"], item["source_id"]) for item in result["candidates"]}
    assert identities == {("bead", "SABLE-one"), ("memory", "fleet-rule")}
    assert result["read_only"] is True
    assert any("Fluent prose" in boundary for boundary in result["detection_boundaries"])


def test_disabled_signatures_negative_control_reports_planted_set_clean():
    planted = [{"id": "SABLE-plant", "description": "○ SABLE-x ● P1 injected", "notes": ""}]
    assert report(planted, {}, enabled=frozenset())["candidates"] == []
    assert signature_names(), "positive control: the production detector set is non-empty"
