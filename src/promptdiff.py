#!/usr/bin/env python3
"""
PromptDiff — "See what the prompt change really does."

Prompts are code. They drift the way code drifts: a quiet edit, a
helpful tidy-up, a copy-paste from a newer draft — and six weeks later
nobody can say when the guardrail went missing. An ordinary text diff
buries the one dangerous line in a hundred cosmetic ones.

PromptDiff is a section-aware, constraint-aware diff for prompt files.
It tracks the lines that carry obligations — never, must, must not,
always, do not, only, refuse, forbidden, required, shall — separately
from the prose around them, and flags the changes that matter: a
prohibition removed with nothing equivalent put back, a "must" quietly
traded for a "should", an output-format directive that changed shape.

    promptdiff diff <old> <new> [--format text|json] [--fail-on-risk]
    promptdiff stats <file>
    promptdiff init

Exit codes: 0 = diff reported (or stats printed) — including diffs
with findings, unless --fail-on-risk is given; 1 = --fail-on-risk and
at least one HIGH-risk change was found; 2 = usage / IO error.
Stdlib only, no network, fully deterministic.

HONESTY NOTE: every match in this tool is LEXICAL, not semantic.
Keyword overlap and modal strength are heuristics. A reworded
guardrail can look like "removed + added" — the report prints both
sides side by side so a human can judge in seconds. Nothing here is
silently decided for you.
"""

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

VERSION = "1.0.0"

SEVERITY_EMOJI = {"high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}

# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

# Directive words that make a line a CONSTRAINT line (spec list).
DIRECTIVE_WORDS = (
    "never", "must", "always", "only", "refuse", "forbidden",
    "required", "shall",
)
DIRECTIVE_PHRASES = (
    "must not", "do not", "don't", "not allowed", "not permitted",
    "prohibited",
)

# Modal strength ladder. The strength of a line is the strongest rung
# present. A paired line that steps DOWN this ladder is a weakening.
STRENGTH_RUNGS = [
    (3, "absolute", ("never", "must not", "do not", "don't", "cannot",
                     "can't", "forbidden", "prohibited", "not allowed",
                     "not permitted", "refuse", "refuses", "refused")),
    (2, "strong", ("must", "shall", "required", "requires", "mandatory",
                   "always")),
    (1, "recommendation", ("should", "ought to", "need to", "needs to")),
    (0, "soft", ("try to", "try not to", "preferably", "if possible",
                 "ideally", "please", "consider", "when you can",
                 "whenever possible")),
]

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for",
    "with", "by", "at", "from", "as", "is", "are", "was", "were",
    "be", "been", "it", "its", "this", "that", "these", "those",
    "you", "your", "yours", "we", "our", "they", "their", "he", "she",
    "i", "me", "my", "will", "would", "can", "could", "may", "might",
    "when", "if", "then", "than", "so", "such", "any", "all", "each",
    "every", "no", "not", "do", "does", "did", "have", "has", "had",
    "into", "about", "over", "under", "out", "up", "down", "per",
    "via", "one", "two", "also", "just", "only", "very", "more",
    "most", "other", "same",
}

# Words that belong to the directive machinery itself, not the topic.
MODAL_WORDS = {
    "never", "must", "shall", "should", "always", "refuse", "refuses",
    "refused", "forbidden", "prohibited", "required", "requires",
    "mandatory", "cannot", "ought", "please", "ideally", "preferably",
    "consider", "try", "tries",
}

PROHIBITION_CUES = (
    "never", "must not", "do not", "don't", "forbidden", "prohibited",
    "not allowed", "not permitted", "refuse", "refuses", "cannot",
    "can't",
)

_token_re = re.compile(r"[a-z]+(?:'[a-z]+)?")


def tokens(text):
    return _token_re.findall(text.lower())


def stem(word):
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def keywords(text):
    """Content keywords of a line: no stopwords, no modal machinery."""
    out = set()
    for tok in tokens(text):
        if tok in STOPWORDS or tok in MODAL_WORDS:
            continue
        out.add(stem(tok))
    return out


def keyword_overlap(a, b):
    """Jaccard similarity of two keyword sets (0.0–1.0)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def line_strength(text):
    """(score, label) of the strongest modal rung present in a line."""
    low = " " + text.lower() + " "
    best = (None, None)
    for score, label, phrases in STRENGTH_RUNGS:
        for ph in phrases:
            if " " in ph or "'" in ph:
                if ph in low:
                    return score, label
            elif re.search(r"\b" + re.escape(ph) + r"\b", low):
                return score, label
    return best


def is_constraint_line(text):
    low = text.lower()
    if any(ph in low for ph in DIRECTIVE_PHRASES):
        return True
    toks = set(tokens(text))
    return bool(toks & set(DIRECTIVE_WORDS))


def classify_constraint(text):
    """Classify one line. Returns None if it is not a constraint line.

    A constraint line carries directive language. Polarity is
    'prohibition' when a prohibition cue is present, else
    'requirement'. Weak-modal lines (should / try to / …) count as
    constraint lines too when they also carry a requirement-style
    topic — see pair detection: a softened rewrite of a guardrail is
    exactly what this tool exists to catch, and it usually arrives
    wearing soft words. To make that pairing possible, a line whose
    ONLY directive signal is a soft/recommendation modal is classified
    as a 'weak' constraint.
    """
    low = " " + text.lower() + " "
    strength = line_strength(text)
    strong_signal = is_constraint_line(text)
    if not strong_signal and strength[0] is None:
        return None
    polarity = "requirement"
    for cue in PROHIBITION_CUES:
        if " " in cue or "'" in cue:
            if cue in low:
                polarity = "prohibition"
                break
        elif re.search(r"\b" + re.escape(cue) + r"\b", low):
            polarity = "prohibition"
            break
    modal = None
    if strength[0] is not None:
        for score, _label, phrases in STRENGTH_RUNGS:
            if score != strength[0]:
                continue
            for ph in phrases:
                if " " in ph or "'" in ph:
                    if ph in low:
                        modal = ph
                        break
                elif re.search(r"\b" + re.escape(ph) + r"\b", low):
                    modal = ph
                    break
            break
    display = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text.strip())
    return {
        "text": display,
        "polarity": polarity,
        "strength": strength[0] if strength[0] is not None else 0,
        "strength_label": strength[1] or "soft",
        "modal": modal,
        "keywords": sorted(keywords(text)),
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

_md_header = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_numbered = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]\s+(\S.*)$")
_caps_re = re.compile(r"^[A-Z0-9][A-Z0-9 ,&'’\-:/()]{2,}$")


def _section_title(line):
    """Return the section title if this line starts a section."""
    s = line.strip()
    m = _md_header.match(s)
    if m:
        return m.group(2).strip()
    if _caps_re.match(s) and (s.endswith(":") or " " in s) \
            and not s.endswith(".") and len(s) <= 80:
        return s.rstrip(":").strip()
    m = _numbered.match(s)
    if m:
        rest = m.group(2).strip()
        if len(rest) <= 70 and len(rest.split()) <= 8 \
                and not rest.endswith("."):
            return rest
    return None


def section_key(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def parse_sections(text):
    """Split prompt text into sections. Lines before the first header
    form a section titled '(preamble)' (omitted when empty)."""
    lines = text.split("\n")
    sections = []
    current = {"title": "(preamble)", "key": "(preamble)", "lines": []}
    for line in lines:
        title = _section_title(line)
        if title is not None:
            sections.append(current)
            current = {"title": title, "key": section_key(title),
                       "lines": []}
        else:
            current["lines"].append(line)
    sections.append(current)
    out = []
    for i, sec in enumerate(sections):
        if sec["title"] == "(preamble)" and i == 0 and \
                not any(l.strip() for l in sec["lines"]):
            continue
        # Drop trailing blank lines of each section body.
        body = list(sec["lines"])
        while body and not body[-1].strip():
            body.pop()
        sec = dict(sec)
        sec["lines"] = body
        out.append(sec)
    return out


def normalize_text(text):
    lines = [l.strip().lower() for l in text.split("\n")]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def similarity(a_text, b_text):
    """difflib ratio over normalized text, 0.0–1.0."""
    a = normalize_text(a_text)
    b = normalize_text(b_text)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Counts / profile
# ---------------------------------------------------------------------------

_url_re = re.compile(r"https?://[^\s)\]]+")
_format_re = re.compile(
    r"\b(json|xml|yaml|markdown|schema|format|table|bullet|numbered\s+list|"
    r"respond\s+with|reply\s+with|output\s+format|code\s+block)\b",
    re.IGNORECASE)
_persona_re = re.compile(
    r"\b(you\s+are|act\s+as|your\s+role\s+is|persona|you\s+are\s+an?\s+|"
    r"role\s*:)", re.IGNORECASE)


def profile(text):
    """Single-prompt profile used by `stats` and the counts delta."""
    lines = text.split("\n")
    sections = parse_sections(text)
    constraints = []
    in_fence = False
    example_lines = 0
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            example_lines += 1
        elif re.search(r"\bexample\s*:", line, re.IGNORECASE):
            example_lines += 1
        c = classify_constraint(line)
        if c is not None:
            constraints.append(c)
    urls = _url_re.findall(text)
    norm = normalize_text(text)
    fingerprint = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return {
        "chars": len(text),
        "words": len(text.split()),
        "lines": len(lines) if text else 0,
        "sections": [s["title"] for s in sections],
        "section_count": len(sections),
        "constraints": len(constraints),
        "constraints_prohibition": sum(
            1 for c in constraints if c["polarity"] == "prohibition"),
        "constraints_requirement": sum(
            1 for c in constraints if c["polarity"] == "requirement"),
        "format_directives": sum(
            1 for l in lines if _format_re.search(l)),
        "examples": example_lines,
        "urls": len(urls),
        "url_list": urls,
        "fingerprint": fingerprint,
        "fingerprint_short": fingerprint[:16],
        "unique_tokens": len(set(tokens(text))),
    }


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

PAIR_THRESHOLD = 0.34      # keyword Jaccard for "same topic" pairing
RENAME_THRESHOLD = 0.5     # section-body similarity for a rename pairing
MAX_LISTED_LINES = 20      # per-section line listing cap


def _line_changes(old_lines, new_lines):
    """(added, removed) line lists from a body diff, order preserved."""
    added, removed = [], []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(l for l in old_lines[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(l for l in new_lines[j1:j2])
    return added, removed


def diff_sections(old_secs, new_secs):
    """Match sections old↔new. Returns a dict of section movements."""
    old_by_key = {}
    for i, s in enumerate(old_secs):
        old_by_key.setdefault(s["key"], []).append(i)
    new_by_key = {}
    for j, s in enumerate(new_secs):
        new_by_key.setdefault(s["key"], []).append(j)

    matched = []            # (old_idx, new_idx)
    used_old, used_new = set(), set()
    for key, idxs in old_by_key.items():
        for oi, nj in zip(idxs, new_by_key.get(key, [])):
            matched.append((oi, nj))
            used_old.add(oi)
            used_new.add(nj)

    # Rename pairing: leftover sections whose bodies stayed similar.
    renamed = []
    for oi in range(len(old_secs)):
        if oi in used_old:
            continue
        best, best_sim = None, 0.0
        for nj in range(len(new_secs)):
            if nj in used_new:
                continue
            sim = similarity("\n".join(old_secs[oi]["lines"]),
                             "\n".join(new_secs[nj]["lines"]))
            if sim > best_sim:
                best, best_sim = nj, sim
        if best is not None and best_sim >= RENAME_THRESHOLD:
            renamed.append((oi, best, round(best_sim, 4)))
            used_old.add(oi)
            used_new.add(best)

    changed, unchanged, moved = [], [], []
    for oi, nj in matched:
        o, n = old_secs[oi], new_secs[nj]
        body_sim = similarity("\n".join(o["lines"]), "\n".join(n["lines"]))
        if oi != nj:
            moved.append(o["title"])
        if o["lines"] == n["lines"]:
            unchanged.append(o["title"])
        else:
            added, removed = _line_changes(o["lines"], n["lines"])
            changed.append({
                "title": o["title"], "similarity": round(body_sim, 4),
                "lines_added": len(added), "lines_removed": len(removed),
                "added_lines": added, "removed_lines": removed,
            })
    for oi, nj, sim in renamed:
        o, n = old_secs[oi], new_secs[nj]
        added, removed = _line_changes(o["lines"], n["lines"])
        changed.append({
            "title": n["title"], "renamed_from": o["title"],
            "similarity": sim,
            "lines_added": len(added), "lines_removed": len(removed),
            "added_lines": added, "removed_lines": removed,
        })

    return {
        "added": [new_secs[j]["title"] for j in range(len(new_secs))
                  if j not in used_new],
        "removed": [old_secs[i]["title"] for i in range(len(old_secs))
                    if i not in used_old],
        "renamed": [{"from": old_secs[oi]["title"],
                     "to": new_secs[nj]["title"], "similarity": sim}
                    for oi, nj, sim in renamed],
        "moved": moved,
        "changed": changed,
        "unchanged": unchanged,
    }


def _constraint_lines(text):
    out = []
    for line in text.split("\n"):
        c = classify_constraint(line)
        if c is not None:
            out.append(c)
    return out


def pair_constraints(old_cons, new_cons):
    """Greedily pair removed↔added constraint lines on the same topic.

    Returns (pairs, removed_only, added_only) where pairs are
    (old, new, overlap). Lines that survive verbatim are carried
    unchanged and never reported. Of the rest, pairing is
    best-overlap-first so the closest rewrite wins the match.
    """
    # Siphon off verbatim survivors first (multiset match on text).
    new_pool = list(range(len(new_cons)))
    rest_old = []
    for o in old_cons:
        hit = None
        for j in new_pool:
            if new_cons[j]["text"] == o["text"]:
                hit = j
                break
        if hit is None:
            rest_old.append(o)
        else:
            new_pool.remove(hit)
    rest_new = [new_cons[j] for j in new_pool]
    old_cons, new_cons = rest_old, rest_new

    candidates = []
    for i, o in enumerate(old_cons):
        okw = set(o["keywords"])
        for j, n in enumerate(new_cons):
            ov = keyword_overlap(okw, set(n["keywords"]))
            if ov >= PAIR_THRESHOLD:
                candidates.append((ov, i, j))
    candidates.sort(reverse=True)
    used_o, used_n, pairs = set(), set(), []
    for ov, i, j in candidates:
        if i in used_o or j in used_n:
            continue
        used_o.add(i)
        used_n.add(j)
        pairs.append((old_cons[i], new_cons[j], round(ov, 4)))
    removed_only = [c for i, c in enumerate(old_cons) if i not in used_o]
    added_only = [c for j, c in enumerate(new_cons) if j not in used_n]
    return pairs, removed_only, added_only


def _persona_lines(text):
    return [l.strip() for l in text.split("\n") if _persona_re.search(l)]


def _format_lines(text):
    return [l.strip() for l in text.split("\n") if _format_re.search(l)]


def build_risks(pairs, removed_only, old_text, new_text,
                old_profile, new_profile):
    risks = []

    # HIGH — prohibition removed, nothing equivalent re-added.
    for c in removed_only:
        if c["polarity"] == "prohibition" and c["strength"] >= 2:
            risks.append({
                "severity": "high", "check": "guardrail-removed",
                "message": "Prohibition removed with no equivalent "
                           "re-added — the guardrail is simply gone.",
                "old_line": c["text"], "new_line": None,
            })

    # HIGH — paired constraint weakened (modal stepped down).
    for o, n, ov in pairs:
        if n["strength"] < o["strength"]:
            risks.append({
                "severity": "high", "check": "constraint-weakened",
                "message": f"Constraint weakened: '{o['modal']}' "
                           f"({o['strength_label']}) → '{n['modal']}' "
                           f"({n['strength_label']}).",
                "old_line": o["text"], "new_line": n["text"],
            })

    # MEDIUM — output-format directives changed.
    old_fmt = {normalize_text(l): l for l in _format_lines(old_text)}
    new_fmt = {normalize_text(l): l for l in _format_lines(new_text)}
    if set(old_fmt) != set(new_fmt):
        removed_fmt = [old_fmt[k] for k in sorted(set(old_fmt) - set(new_fmt))]
        added_fmt = [new_fmt[k] for k in sorted(set(new_fmt) - set(old_fmt))]
        risks.append({
            "severity": "medium", "check": "format-directive-changed",
            "message": "Output-format directives changed "
                       f"({len(removed_fmt)} removed, "
                       f"{len(added_fmt)} added).",
            "old_line": removed_fmt[0] if removed_fmt else None,
            "new_line": added_fmt[0] if added_fmt else None,
            "removed_lines": removed_fmt, "added_lines": added_fmt,
        })

    # MEDIUM — persona / role line changed.
    old_per = {normalize_text(l): l for l in _persona_lines(old_text)}
    new_per = {normalize_text(l): l for l in _persona_lines(new_text)}
    if set(old_per) != set(new_per):
        removed_per = [old_per[k]
                       for k in sorted(set(old_per) - set(new_per))]
        added_per = [new_per[k]
                     for k in sorted(set(new_per) - set(old_per))]
        risks.append({
            "severity": "medium", "check": "persona-changed",
            "message": "Persona/role wording changed — the agent's "
                       "identity line was edited.",
            "old_line": removed_per[0] if removed_per else None,
            "new_line": added_per[0] if added_per else None,
            "removed_lines": removed_per, "added_lines": added_per,
        })

    # LOW — pure size drift over 30% either way.
    oc, nc = old_profile["chars"], new_profile["chars"]
    if oc > 0:
        drift = (nc - oc) / oc
        if abs(drift) > 0.30:
            risks.append({
                "severity": "low", "check": "size-drift",
                "message": f"Prompt size changed by {drift:+.0%} "
                           f"({oc} → {nc} chars).",
                "old_line": None, "new_line": None,
            })
    return risks


def compute_diff(old_text, new_text, old_name="(old)", new_name="(new)"):
    old_profile = profile(old_text)
    new_profile = profile(new_text)
    old_secs = parse_sections(old_text)
    new_secs = parse_sections(new_text)
    sec_report = diff_sections(old_secs, new_secs)

    old_cons = _constraint_lines(old_text)
    new_cons = _constraint_lines(new_text)
    pairs, removed_only, added_only = pair_constraints(old_cons, new_cons)
    modified = [{
        "old": o["text"], "new": n["text"], "overlap": ov,
        "old_strength": o["strength_label"],
        "new_strength": n["strength_label"],
    } for o, n, ov in pairs]

    risks = build_risks(pairs, removed_only, old_text, new_text,
                        old_profile, new_profile)
    for r in risks:
        # Family severity tier alongside the native label
        # (CONVENTIONS.md): high -> HIGH, medium/low -> INFO.
        r["tier"] = "HIGH" if r["severity"] == "high" else "INFO"

    def delta(key):
        return {"old": old_profile[key], "new": new_profile[key],
                "delta": new_profile[key] - old_profile[key]}

    sim = similarity(old_text, new_text)
    return {
        "tool": "promptdiff", "version": VERSION, "schema_version": 1,
        "findings_summary": {
            "count": len(risks),
            "highest_severity": (
                "HIGH" if any(r["severity"] == "high" for r in risks)
                else "INFO" if risks else "NONE"),
        },
        "old": old_name, "new": new_name,
        "identical": normalize_text(old_text) == normalize_text(new_text),
        "similarity": round(sim, 4),
        "counts": {
            "chars": delta("chars"), "words": delta("words"),
            "lines": delta("lines"),
            "sections": delta("section_count"),
            "constraints": delta("constraints"),
            "constraints_added": len(added_only),
            "constraints_removed": len(removed_only),
            "examples": delta("examples"), "urls": delta("urls"),
            "format_directives": delta("format_directives"),
        },
        "sections": sec_report,
        "constraints": {
            "added": added_only, "removed": removed_only,
            "modified": modified,
        },
        "risks": risks,
        "high_risks": sum(1 for r in risks if r["severity"] == "high"),
    }


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _truncate(lines, cap=MAX_LISTED_LINES):
    if len(lines) <= cap:
        return list(lines), 0
    return list(lines[:cap]), len(lines) - cap


def print_diff_text(r):
    print("=" * 68)
    print("PromptDiff — diff report")
    print("=" * 68)
    print(f"Old:        {r['old']}")
    print(f"New:        {r['new']}")
    state = "IDENTICAL" if r["identical"] else "CHANGED"
    print(f"Similarity: {r['similarity']:.1%} · {state}")
    print()
    c = r["counts"]
    print("## Counts")
    print(f"  {'metric':<18}{'old':>8}{'new':>8}{'delta':>8}")
    for label, key in (("chars", "chars"), ("words", "words"),
                       ("lines", "lines"), ("sections", "sections"),
                       ("constraints", "constraints"),
                       ("examples", "examples"), ("urls", "urls"),
                       ("format lines", "format_directives")):
        d = c[key]
        print(f"  {label:<18}{d['old']:>8}{d['new']:>8}{d['delta']:>+8}")
    print(f"  constraints added:   {c['constraints_added']}")
    print(f"  constraints removed: {c['constraints_removed']}")
    print()

    s = r["sections"]
    print("## Sections")
    if s["added"]:
        print(f"  added:     {', '.join(s['added'])}")
    if s["removed"]:
        print(f"  removed:   {', '.join(s['removed'])}")
    for rn in s["renamed"]:
        print(f"  renamed:   {rn['from']} → {rn['to']} "
              f"({rn['similarity']:.0%} body match)")
    if s["moved"]:
        print(f"  moved:     {', '.join(s['moved'])}")
    for ch in s["changed"]:
        title = ch["title"]
        if "renamed_from" in ch:
            title += f" (was: {ch['renamed_from']})"
        print(f"  changed:   {title}  (+{ch['lines_added']}/"
              f"−{ch['lines_removed']} lines, "
              f"{ch['similarity']:.0%} similar)")
        removed, extra_r = _truncate(
            [l for l in ch["removed_lines"] if l.strip()])
        added, extra_a = _truncate(
            [l for l in ch["added_lines"] if l.strip()])
        for l in removed:
            print(f"    - {l.strip()}")
        if extra_r:
            print(f"    - … and {extra_r} more removed line(s)")
        for l in added:
            print(f"    + {l.strip()}")
        if extra_a:
            print(f"    + … and {extra_a} more added line(s)")
    if s["unchanged"]:
        print(f"  unchanged: {', '.join(s['unchanged'])}")
    if not any((s["added"], s["removed"], s["renamed"], s["changed"])):
        print("  (no section changes)")
    print()

    cons = r["constraints"]
    print("## Constraints")
    if cons["removed"]:
        print(f"  REMOVED ({len(cons['removed'])}):")
        for cc in cons["removed"]:
            print(f"    - [{cc['polarity']} · {cc['strength_label']}] "
                  f"{cc['text']}")
    if cons["added"]:
        print(f"  ADDED ({len(cons['added'])}):")
        for cc in cons["added"]:
            print(f"    + [{cc['polarity']} · {cc['strength_label']}] "
                  f"{cc['text']}")
    if cons["modified"]:
        print(f"  REWRITTEN ({len(cons['modified'])}):")
        for m in cons["modified"]:
            print(f"    ~ [{m['old_strength']} → {m['new_strength']}] "
                  f"{m['old']}")
            print(f"      → {m['new']}")
    if not (cons["removed"] or cons["added"] or cons["modified"]):
        print("  (no constraint changes)")
    print()

    print("## Risks")
    if r["risks"]:
        for risk in r["risks"]:
            emoji = SEVERITY_EMOJI[risk["severity"]]
            print(f"  {emoji} {risk['severity'].upper():<6} "
                  f"[{risk['check']}] {risk['message']}")
            if risk.get("old_line"):
                print(f"             - {risk['old_line']}")
            if risk.get("new_line"):
                print(f"             + {risk['new_line']}")
    else:
        print("  (no risk findings)")
    print()
    if r["high_risks"]:
        print(f"⚠️ {r['high_risks']} HIGH-risk change(s) — review "
              "before this prompt ships.")
    else:
        print("✅ No HIGH-risk changes.")


def print_stats_text(p, name):
    print("=" * 68)
    print("PromptDiff — prompt profile")
    print("=" * 68)
    print(f"File:        {name}")
    print(f"Fingerprint: {p['fingerprint_short']}… "
          "(sha256 of normalized text)")
    print(f"Size:        {p['chars']} chars · {p['words']} words · "
          f"{p['lines']} lines · {p['unique_tokens']} unique tokens")
    print(f"Sections:    {p['section_count']} — "
          f"{', '.join(p['sections']) if p['sections'] else '(none)'}")
    print(f"Constraints: {p['constraints']} "
          f"({p['constraints_prohibition']} prohibitions, "
          f"{p['constraints_requirement']} requirements)")
    print(f"Format directives: {p['format_directives']} line(s)")
    print(f"Examples:    {p['examples']} line(s) · "
          f"URLs: {p['urls']}")


# ---------------------------------------------------------------------------
# init samples
# ---------------------------------------------------------------------------

SAMPLE_OLD = """\
# Research Helper Prompt (v1)

## Role
You are a careful research helper for the Acme team wiki.

## Rules
- Never invent a citation, quote, or source.
- You must link every claim to a source the user provided.
- Always say when you are unsure.

## Output
Answer in short paragraphs.
"""

SAMPLE_NEW = """\
# Research Helper Prompt (v2)

## Role
You are a careful research helper for the Acme team wiki.

## Rules
- You must link every claim to a source the user provided.
- Always say when you are unsure.

## Output
Answer in short paragraphs, and add a JSON summary block at the end.
"""

EXPECTED_TEMPLATE = """\
# What `init` just made

Two versions of a small research-helper prompt, and the one change
that matters between them: **v2 quietly dropped the rule
"Never invent a citation, quote, or source."**

Try it:

    promptdiff diff old.md new.md
    promptdiff diff old.md new.md --fail-on-risk   # exits 1
    promptdiff stats old.md

You should see one 🟠 HIGH `guardrail-removed` risk citing the dropped
line, the Rules section marked changed, and the constraint count
delta at -1. That is the whole point of PromptDiff: an ordinary diff
shows two files that look almost the same; this one points at the
missing guardrail.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read(path):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_arg(path):
    """Read a prompt file, or stdin when path is '-' (pipe
    composability — CONVENTIONS.md)."""
    if path == "-":
        return sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n")
    return _read(path)


def _label(path):
    return "<stdin>" if path == "-" else path


# ---------------------------------------------------------------------------
# SARIF output (SARIF 2.1.0 — see CONVENTIONS.md)
# ---------------------------------------------------------------------------

def _sarif_level(tier, severity):
    """Tier HIGH -> error; native medium -> warning; else note."""
    if tier == "HIGH":
        return "error"
    if severity == "medium":
        return "warning"
    return "note"


def _sarif_rule_name(rule_id):
    return "".join(p.capitalize() for p in re.split(
        r"[^A-Za-z0-9]+", rule_id) if p) or "Finding"


def _find_line(text, needle):
    """1-based line number of the line whose stripped form equals the
    needle's stripped form, or None."""
    if not needle:
        return None
    want = needle.strip()
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip() == want:
            return i
    return None


def build_sarif(r, old_text, new_text, old_label, new_label):
    """Build a SARIF 2.1.0 log from a compute_diff result. A risk is
    located in the new file when it has a new-side line, else in the
    old file (a removed guardrail lives there), else unlocated."""
    rules, seen = [], set()
    results = []
    for risk in r["risks"]:
        level = _sarif_level(risk["tier"], risk["severity"])
        if risk["check"] not in seen:
            seen.add(risk["check"])
            rules.append({
                "id": risk["check"],
                "name": _sarif_rule_name(risk["check"]),
                "shortDescription": {
                    "text": f"PromptDiff risk: {risk['check']}"},
                "defaultConfiguration": {"level": level},
                "properties": {"severity": risk["severity"],
                               "tier": risk["tier"]},
            })
        res = {"ruleId": risk["check"], "level": level,
               "message": {"text": risk["message"]}}
        uri = line = None
        if risk.get("new_line"):
            uri, line = new_label, _find_line(new_text,
                                              risk["new_line"])
        elif risk.get("old_line"):
            uri, line = old_label, _find_line(old_text,
                                              risk["old_line"])
        if uri:
            loc = {"artifactLocation": {"uri": uri}}
            if line:
                loc["region"] = {"startLine": line}
            res["locations"] = [{"physicalLocation": loc}]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "promptdiff",
                "version": VERSION,
                "informationUri":
                    "https://github.com/maticcretic-commits/promptdiff",
                "rules": rules,
            }},
            "results": results,
        }],
    }


def _print_error(e):
    print(f"error: {e}", file=sys.stderr)
    code = "file_not_found" if isinstance(e, FileNotFoundError) \
        else "read_error"
    print(json.dumps({"error": {"code": code, "message": str(e)}}),
          file=sys.stderr)


def cmd_diff(args):
    if args.old == "-" and args.new == "-":
        msg = "only one side of a diff may come from stdin"
        print(f"error: {msg}", file=sys.stderr)
        print(json.dumps({"error": {"code": "invalid_args",
                                    "message": msg}}),
              file=sys.stderr)
        return 2
    try:
        old_text = _read_arg(args.old)
        new_text = _read_arg(args.new)
    except OSError as e:
        _print_error(e)
        return 2
    r = compute_diff(old_text, new_text, _label(args.old),
                     _label(args.new))
    if args.format == "sarif":
        print(json.dumps(build_sarif(r, old_text, new_text,
                                     _label(args.old),
                                     _label(args.new)),
                         indent=2, ensure_ascii=False))
    elif args.format == "json":
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print_diff_text(r)
    if args.fail_on_risk and r["high_risks"]:
        return 1
    return 0


def cmd_stats(args):
    try:
        text = _read_arg(args.file)
    except OSError as e:
        _print_error(e)
        return 2
    p = profile(text)
    if args.format == "json":
        print(json.dumps({"tool": "promptdiff", "version": VERSION,
                          "schema_version": 1,
                          "file": _label(args.file), **p},
                         indent=2, ensure_ascii=False))
    else:
        print_stats_text(p, _label(args.file))
    return 0


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path.cwd()
    targets = {base / "old.md": SAMPLE_OLD, base / "new.md": SAMPLE_NEW,
               base / "EXPECTED.md": EXPECTED_TEMPLATE}
    for p in targets:
        if p.exists() and not args.force:
            print(f"{p} exists — use --force to overwrite")
            return 2
    for p, content in targets.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    print(f"created old.md, new.md and EXPECTED.md in {base}")
    print("try it:  promptdiff diff old.md new.md --fail-on-risk")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="promptdiff",
        description="Section-aware, constraint-aware diff for prompt "
                    "files. See what the prompt change really does.")
    ap.add_argument("--version", action="version",
                    version=f"promptdiff {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color output (this tool emits "
                         "no color; accepted harmlessly)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diff", help="diff two prompt files")
    d.add_argument("old", help="the old (baseline) prompt file "
                               "('-' reads stdin; one side only)")
    d.add_argument("new", help="the new prompt file")
    d.add_argument("--format", choices=("text", "json", "sarif"),
                   default="text")
    d.add_argument("--fail-on-risk", action="store_true",
                   help="exit 1 if any HIGH-risk change is found")
    d.set_defaults(fn=cmd_diff)

    s = sub.add_parser("stats", help="profile a single prompt file")
    s.add_argument("file", help="the prompt file to profile "
                                "('-' reads stdin)")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s.set_defaults(fn=cmd_stats)

    i = sub.add_parser("init", help="scaffold a sample old/new pair "
                                    "+ EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample "
                                 "files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
