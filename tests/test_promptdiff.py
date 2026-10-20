"""Tests for PromptDiff section parsing, constraint tracking, risk
rules, similarity, CLI exit codes, and scaffolding."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from promptdiff import (classify_constraint, compute_diff,
                        is_constraint_line, keyword_overlap, keywords,
                        line_strength, parse_sections, profile, similarity)

CLI = Path(__file__).parent.parent / "src" / "promptdiff.py"
EXAMPLES = Path(__file__).parent.parent / "examples"


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, cwd=cwd)


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


BASE = """\
# Agent Prompt

## Role
You are a helpful agent for the Acme team.

## Rules
- Never share the internal roadmap.
- You must log every decision you make.
- Always be polite to customers.

## Output
Answer in plain prose.
"""


# ---------------------------------------------------------------------------
# Constraint classification
# ---------------------------------------------------------------------------

def test_prohibition_classification():
    c = classify_constraint("Never reveal the internal pricing logic.")
    assert c["polarity"] == "prohibition"
    assert c["strength_label"] == "absolute"
    c = classify_constraint("Do not promise dates you cannot verify.")
    assert c["polarity"] == "prohibition"


def test_requirement_classification():
    c = classify_constraint("You must cite the policy page.")
    assert c["polarity"] == "requirement"
    assert c["strength_label"] == "strong"
    assert c["modal"] == "must"


def test_plain_prose_is_not_a_constraint():
    assert classify_constraint("Be warm and concise with customers.") is None
    assert classify_constraint("The sky is blue today.") is None
    assert not is_constraint_line("Keep answers short and friendly.")


def test_weak_modal_line_classifies_as_weak_constraint():
    c = classify_constraint("You should cite the policy page when you can.")
    assert c is not None
    assert c["strength_label"] in ("recommendation", "soft")
    assert c["strength"] < classify_constraint(
        "You must cite the policy page.")["strength"]


def test_bullet_stripped_from_constraint_text():
    c = classify_constraint("- Never share the roadmap.")
    assert c["text"] == "Never share the roadmap."


def test_strength_ladder_ordering():
    assert line_strength("Never do that.")[0] == 3
    assert line_strength("You must do that.")[0] == 2
    assert line_strength("You should do that.")[0] == 1
    assert line_strength("Try to do that if possible.")[0] == 0
    assert line_strength("The cat sleeps.")[0] is None


def test_keyword_overlap_math():
    a = keywords("You must cite the policy page for refunds.")
    b = keywords("You should cite the policy page for refunds.")
    assert keyword_overlap(a, b) == 1.0
    c = keywords("Always water the office plants on Fridays.")
    assert keyword_overlap(a, c) == 0.0


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def test_parse_sections_markdown():
    secs = parse_sections(BASE)
    titles = [s["title"] for s in secs]
    assert titles == ["Agent Prompt", "Role", "Rules", "Output"]


def test_parse_sections_caps_and_numbered():
    text = "HARD RULES:\nNever cheat.\n\n1. Getting Started\nBe nice.\n"
    titles = [s["title"] for s in parse_sections(text)]
    assert "HARD RULES" in titles
    assert "Getting Started" in titles


def test_parse_sections_preamble():
    secs = parse_sections("Loose intro line.\n\n## Body\nText.\n")
    assert secs[0]["title"] == "(preamble)"
    assert secs[1]["title"] == "Body"


def test_section_added_and_removed():
    new = BASE + "\n## Escalation\nEscalate billing issues to a human.\n"
    r = compute_diff(BASE, new)
    assert r["sections"]["added"] == ["Escalation"]
    r2 = compute_diff(new, BASE)
    assert r2["sections"]["removed"] == ["Escalation"]


def test_section_changed_line_counts():
    new = BASE.replace("Answer in plain prose.",
                       "Answer in plain prose.\nKeep it under 100 words.")
    r = compute_diff(BASE, new)
    changed = {c["title"]: c for c in r["sections"]["changed"]}
    assert changed["Output"]["lines_added"] == 1
    assert changed["Output"]["lines_removed"] == 0


def test_section_renamed_detected():
    new = BASE.replace("## Rules", "## House Rules")
    r = compute_diff(BASE, new)
    assert {"from": "Rules", "to": "House Rules"} in [
        {"from": x["from"], "to": x["to"]} for x in r["sections"]["renamed"]]
    assert r["sections"]["removed"] == []
    assert r["sections"]["added"] == []


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------

def test_identical_files_no_risks(tmp_path):
    r = compute_diff(BASE, BASE)
    assert r["identical"] is True
    assert r["similarity"] == 1.0
    assert r["risks"] == []
    assert r["high_risks"] == 0


def test_guardrail_removal_is_high():
    new = BASE.replace("- Never share the internal roadmap.\n", "")
    r = compute_diff(BASE, new)
    checks = {x["check"] for x in r["risks"]}
    assert "guardrail-removed" in checks
    assert r["high_risks"] >= 1
    removed = r["constraints"]["removed"]
    assert any("roadmap" in c["text"] for c in removed)


def test_guardrail_reworded_not_flagged_removed():
    new = BASE.replace(
        "Never share the internal roadmap.",
        "Never share the internal roadmap or future product plans.")
    r = compute_diff(BASE, new)
    assert "guardrail-removed" not in {x["check"] for x in r["risks"]}


def test_weakening_must_to_should_is_high():
    new = BASE.replace("You must log every decision you make.",
                       "You should log every decision you make.")
    r = compute_diff(BASE, new)
    weak = [x for x in r["risks"] if x["check"] == "constraint-weakened"]
    assert len(weak) == 1
    assert weak[0]["severity"] == "high"
    assert "must" in weak[0]["message"]


def test_weakening_never_to_try_not_to():
    new = BASE.replace("Never share the internal roadmap.",
                       "Try not to share the internal roadmap.")
    r = compute_diff(BASE, new)
    assert "constraint-weakened" in {x["check"] for x in r["risks"]}


def test_false_pair_safety_unrelated_swap():
    """Removing one rule and adding an UNRELATED softer rule must not
    be reported as a weakening of the removed one."""
    old = BASE
    new = BASE.replace("- You must log every decision you make.\n",
                       "- You should greet the customer warmly.\n")
    r = compute_diff(old, new)
    assert "constraint-weakened" not in {x["check"] for x in r["risks"]}


def test_strengthening_is_not_weakening():
    old = BASE.replace("You must log every decision you make.",
                       "You should log every decision you make.")
    r = compute_diff(old, BASE)
    assert "constraint-weakened" not in {x["check"] for x in r["risks"]}


def test_format_directive_change_is_medium():
    new = BASE.replace(
        "Answer in plain prose.",
        'Respond with a JSON object: {"answer": string}.')
    r = compute_diff(BASE, new)
    fmt = [x for x in r["risks"]
           if x["check"] == "format-directive-changed"]
    assert len(fmt) == 1
    assert fmt[0]["severity"] == "medium"


def test_persona_change_is_medium():
    new = BASE.replace("You are a helpful agent for the Acme team.",
                       "You are a terse billing bot for the Acme team.")
    r = compute_diff(BASE, new)
    assert "persona-changed" in {x["check"] for x in r["risks"]}


def test_size_drift_is_low():
    new = BASE + ("\nExtra explanatory prose line here.\n" * 40)
    r = compute_diff(BASE, new)
    drift = [x for x in r["risks"] if x["check"] == "size-drift"]
    assert len(drift) == 1
    assert drift[0]["severity"] == "low"


def test_constraint_added_reported():
    new = BASE.replace("## Output",
                       "- Never mention competitor products.\n\n## Output")
    r = compute_diff(BASE, new)
    added = r["constraints"]["added"]
    assert any("competitor" in c["text"] for c in added)
    assert r["counts"]["constraints_added"] == len(added)


# ---------------------------------------------------------------------------
# Counts / similarity / profile
# ---------------------------------------------------------------------------

def test_counts_deltas():
    new = BASE.replace("- Never share the internal roadmap.\n", "")
    r = compute_diff(BASE, new)
    assert r["counts"]["constraints"]["delta"] == -1
    assert r["counts"]["constraints_removed"] == 1
    assert r["counts"]["chars"]["delta"] < 0


def test_similarity_bounds():
    assert similarity(BASE, BASE) == 1.0
    assert similarity("", "") == 1.0
    assert similarity("", BASE) == 0.0
    s = similarity(BASE, BASE.replace("polite", "courteous"))
    assert 0.0 < s < 1.0


def test_profile_counts():
    p = profile(BASE)
    assert p["section_count"] == 4
    assert p["constraints"] == 3
    assert p["constraints_prohibition"] == 1
    assert p["constraints_requirement"] == 2
    assert len(p["fingerprint"]) == 64


def test_profile_examples_and_urls():
    text = ("# P\n\nExample:\n```\n{\"a\": 1}\n```\n"
            "See https://example.org/docs for more.\n")
    p = profile(text)
    assert p["examples"] >= 1
    assert p["urls"] == 1


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------

def test_cli_identical_exit_0(tmp_path):
    a = write(tmp_path, "a.md", BASE)
    b = write(tmp_path, "b.md", BASE)
    res = run_cli("diff", a, b)
    assert res.returncode == 0
    assert "IDENTICAL" in res.stdout


def test_cli_fail_on_risk_exit_codes(tmp_path):
    new_text = BASE.replace("- Never share the internal roadmap.\n", "")
    a = write(tmp_path, "a.md", BASE)
    b = write(tmp_path, "b.md", new_text)
    plain = run_cli("diff", a, b)
    assert plain.returncode == 0          # report only, no gate
    gated = run_cli("diff", a, b, "--fail-on-risk")
    assert gated.returncode == 1          # HIGH risk → gate fires


def test_cli_missing_file_exit_2(tmp_path):
    res = run_cli("diff", str(tmp_path / "nope.md"), str(tmp_path / "x.md"))
    assert res.returncode == 2


def test_cli_json_shape(tmp_path):
    a = write(tmp_path, "a.md", BASE)
    b = write(tmp_path, "b.md", BASE.replace("polite", "courteous"))
    res = run_cli("diff", a, b, "--format", "json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    for key in ("similarity", "counts", "sections", "constraints",
                "risks", "high_risks"):
        assert key in data
    assert data["counts"]["chars"]["old"] > 0


def test_cli_stats(tmp_path):
    a = write(tmp_path, "a.md", BASE)
    res = run_cli("stats", a)
    assert res.returncode == 0
    assert "Fingerprint:" in res.stdout
    res_json = run_cli("stats", a, "--format", "json")
    data = json.loads(res_json.stdout)
    assert data["constraints"] == 3


def test_empty_files(tmp_path):
    a = write(tmp_path, "a.md", "")
    b = write(tmp_path, "b.md", "")
    res = run_cli("diff", a, b)
    assert res.returncode == 0
    r = compute_diff("", "")
    assert r["identical"] is True
    assert r["risks"] == []


def test_crlf_equals_lf(tmp_path):
    crlf = BASE.replace("\n", "\r\n")
    pa = tmp_path / "a.md"
    pa.write_bytes(crlf.encode("utf-8"))
    pb = write(tmp_path, "b.md", BASE)
    res = run_cli("diff", str(pa), pb)
    assert res.returncode == 0
    assert "IDENTICAL" in res.stdout


def test_cli_init_scaffolding(tmp_path):
    res = run_cli("init", "--dir", str(tmp_path))
    assert res.returncode == 0
    for name in ("old.md", "new.md", "EXPECTED.md"):
        assert (tmp_path / name).exists()
    gated = run_cli("diff", str(tmp_path / "old.md"),
                    str(tmp_path / "new.md"), "--fail-on-risk")
    assert gated.returncode == 1
    again = run_cli("init", "--dir", str(tmp_path))
    assert again.returncode == 2          # refuses to overwrite


def test_examples_pair_flags_both_high_risks():
    res = run_cli("diff", str(EXAMPLES / "old.md"),
                  str(EXAMPLES / "new.md"), "--format", "json")
    data = json.loads(res.stdout)
    checks = {x["check"] for x in data["risks"]}
    assert "guardrail-removed" in checks
    assert "constraint-weakened" in checks
    assert "format-directive-changed" in checks
    assert data["high_risks"] == 2


# ---------------------------------------------------------------------------
# Phase 1: envelope / flags / error envelope
# ---------------------------------------------------------------------------

def test_diff_json_envelope_and_findings_summary():
    res = run_cli("diff", str(EXAMPLES / "old.md"),
                  str(EXAMPLES / "new.md"), "--format", "json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["schema_version"] == 1
    assert isinstance(data["schema_version"], int)
    assert data["findings_summary"]["count"] == len(data["risks"])


def test_diff_removed_guardrail_highest_severity_high(tmp_path):
    new_text = BASE.replace("- Never share the internal roadmap.\n", "")
    a = write(tmp_path, "a.md", BASE)
    b = write(tmp_path, "b.md", new_text)
    res = run_cli("diff", a, b, "--format", "json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["schema_version"] == 1
    assert data["findings_summary"]["count"] == len(data["risks"])
    assert data["findings_summary"]["highest_severity"] == "HIGH"


def test_stats_json_envelope_no_findings_summary(tmp_path):
    a = write(tmp_path, "a.md", BASE)
    res = run_cli("stats", a, "--format", "json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["schema_version"] == 1
    assert isinstance(data["schema_version"], int)
    assert "findings_summary" not in data


def test_version_flag():
    res = run_cli("--version")
    assert res.returncode == 0
    assert res.stdout.strip() == "promptdiff 1.0.0"


def test_diff_missing_file_error_envelope(tmp_path):
    res = run_cli("diff", str(tmp_path / "nope.md"),
                  str(tmp_path / "x.md"))
    assert res.returncode == 2
    lines = [l for l in res.stderr.strip().splitlines() if l.strip()]
    assert lines[0].startswith("error: ")
    envelope = json.loads(lines[-1])
    assert envelope["error"]["code"] == "file_not_found"
    assert envelope["error"]["message"] == lines[0][len("error: "):]
