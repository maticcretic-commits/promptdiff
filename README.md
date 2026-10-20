# PromptDiff 🔍

**See what the prompt change really does.**

Prompts are code. They drift the way code drifts: a quiet edit, a
helpful tidy-up, a copy-paste from a newer draft — and six weeks later
nobody can say when the guardrail went missing. An ordinary text diff
is no help: it buries the one dangerous line in a hundred cosmetic
ones, and it counts a deleted "never" the same as a moved comma.

PromptDiff is a section-aware, constraint-aware diff for prompt
files. It tracks the lines that carry obligations — *never, must,
must not, always, do not, only, refuse, forbidden, required, shall* —
separately from the prose around them, and flags the changes that
actually change behaviour: a prohibition removed with nothing
equivalent put back, a "must" quietly traded for a "should", an
output-format directive that changed shape.

Offline and dependency-free: stdlib-only Python, no network, no
account, fully deterministic. Same inputs, same report, every time.

```bash
python src/promptdiff.py diff prompt-v1.md prompt-v2.md
python src/promptdiff.py diff prompt-v1.md prompt-v2.md --fail-on-risk
python src/promptdiff.py stats prompt-v2.md
```

## The commands

| Command | What it does |
|---------|--------------|
| `diff <old> <new>` | Full change report: section movements, per-section line diffs, constraint lines added / removed / rewritten, risk findings with the exact lines cited, and a counts delta table. |
| `diff … --fail-on-risk` | Same report, but exits `1` when any **HIGH**-risk change is found — drop it in CI and a weakened guardrail fails the build. |
| `diff … --format json` | Machine-readable report: similarity, counts, sections, constraints, risks. |
| `stats <file>` | One prompt's profile: chars / words / lines, section list, constraint counts by polarity, format directives, and a sha256 fingerprint of the normalized text. |
| `init` | Scaffold a sample `old.md` / `new.md` pair + `EXPECTED.md`. |

Exit codes: `0` = report printed (findings included — a diff is a
report, not a verdict, unless you ask for the gate), `1` =
`--fail-on-risk` and at least one HIGH-risk change, `2` = usage or
IO error.

## How it reads a prompt

**Sections.** Files are split on markdown headers (`#`–`######`),
ALL-CAPS titles, and numbered titles (`1. Core Rules`). Sections are
matched between versions; the report lists them as added, removed,
renamed (same body, new title), moved, changed (with per-section
line counts and similarity), or unchanged.

**Constraints.** A line carrying directive language is a *constraint
line*. Each is classified by polarity — **prohibition** ("never",
"do not", "forbidden", …) vs **requirement** ("must", "shall",
"always", …) — and by modal strength:

| Strength | Words |
|----------|-------|
| absolute | never, must not, do not, don't, cannot, forbidden, prohibited, refuse |
| strong | must, shall, required, mandatory, always |
| recommendation | should, ought to, need to |
| soft | try to, try not to, preferably, if possible, ideally, please, when you can |

**Pairing.** A removed constraint line and an added constraint line
on the same topic (matched by normalized keyword overlap) are treated
as a *rewrite*, not as a disappearance plus an unrelated arrival.
Pairs are what make the weakening check possible — and what stop a
reworded guardrail from being reported as deleted.

## Risk rules

| Check | Severity | Fires when |
|-------|----------|------------|
| `guardrail-removed` | 🟠 high | A strong prohibition is removed and no equivalent constraint is re-added (no same-topic pair). |
| `constraint-weakened` | 🟠 high | A paired constraint steps **down** the strength ladder: "never" → "try not to", "must" → "should". |
| `format-directive-changed` | 🟡 medium | Output-format directive lines (JSON, schema, format, respond-with, …) are added or removed. |
| `persona-changed` | 🟡 medium | The persona/role line ("You are…", "Act as…", "Your role is…") is edited. |
| `size-drift` | 🔵 low | Total size changes by more than 30% in either direction. |

Every finding cites the exact line(s) it is about. A HIGH finding is
a review prompt, not a conviction — see the honesty note.

## Honesty note

Every match in this tool is **lexical, not semantic**. Keyword
overlap and modal strength are heuristics, and heuristics have edges:

- A guardrail reworded beyond keyword recognition looks like
  "removed + added" — which is why the report prints both sides
  adjacently, so a human can judge in seconds.
- An unrelated soft line cannot be paired with a removed hard line
  unless they genuinely share topic keywords (pairing needs a
  keyword-overlap threshold, currently 0.34 Jaccard).
- "Format directive" means the line mentions format vocabulary; it
  is a pointer for review, not a parse of your output contract.

What PromptDiff guarantees is narrower and more useful than magic:
no constraint line changes silently. Every addition, removal and
rewrite is listed, with its strength, next to the prose diff.

## Quick start

```bash
# 1. Scaffold the sample pair and see a guardrail go missing:
promptdiff init
promptdiff diff old.md new.md
promptdiff diff old.md new.md --fail-on-risk   # exits 1

# 2. Gate a prompt change in CI:
promptdiff diff prompts/prod-v41.md prompts/prod-v42.md --fail-on-risk

# 3. Profile one prompt:
promptdiff stats prompts/prod-v42.md
```

## Example

From `examples/` — a support-agent prompt, v1 → v2. The edit looks
routine: a JSON output format was added and the tone section was
re-wrapped. It also **deleted the pricing prohibition** and
**softened "must cite the policy" into "should cite the policy"**.
Real captured output:

```
$ promptdiff diff examples/old.md examples/new.md
====================================================================
PromptDiff — diff report
====================================================================
Old:        examples/old.md
New:        examples/new.md
Similarity: 43.4% · CHANGED

## Counts
  metric                 old     new   delta
  chars                  726     719      -7
  words                  121     122      +1
  lines                   26      26      +0
  sections                 5       5      +0
  constraints              4       4      +0
  examples                 0       0      +0
  urls                     0       0      +0
  format lines             0       2      +2
  constraints added:   1
  constraints removed: 1

## Sections
  changed:   Hard Rules  (+1/−2 lines, 76% similar)
    - - Never reveal internal pricing logic, discount formulas, or cost breakdowns.
    - - You must cite the policy page for every refund decision.
    + - You should cite the policy page for refund decisions when you can.
  changed:   Tone  (+3/−3 lines, 94% similar)
    - Be warm and concise. Explain things in plain language, like you would
    - to a neighbour. Keep answers under 150 words unless the customer asks
    - for detail.
    + Be warm and concise. Explain things in plain language, as you would to
    + a neighbour. Keep answers under 150 words unless the customer asks for
    + more detail.
  changed:   Output  (+2/−1 lines, 36% similar)
    - Answer in plain prose. End with one follow-up question.
    + Respond with a JSON object: {"answer": string, "follow_up": string}.
    + Do not add any text outside the JSON object.
  unchanged: Support Agent Prompt — Acme Helpdesk, Role

## Constraints
  REMOVED (1):
    - [prohibition · absolute] Never reveal internal pricing logic, discount formulas, or cost breakdowns.
  ADDED (1):
    + [prohibition · absolute] Do not add any text outside the JSON object.
  REWRITTEN (1):
    ~ [strong → recommendation] You must cite the policy page for every refund decision.
      → You should cite the policy page for refund decisions when you can.

## Risks
  🟠 HIGH   [guardrail-removed] Prohibition removed with no equivalent re-added — the guardrail is simply gone.
             - Never reveal internal pricing logic, discount formulas, or cost breakdowns.
  🟠 HIGH   [constraint-weakened] Constraint weakened: 'must' (strong) → 'should' (recommendation).
             - You must cite the policy page for every refund decision.
             + You should cite the policy page for refund decisions when you can.
  🟡 MEDIUM [format-directive-changed] Output-format directives changed (0 removed, 2 added).
             + Do not add any text outside the JSON object.

⚠️ 2 HIGH-risk change(s) — review before this prompt ships.
```

Note the trap the counts set: total constraints are 4 → 4, delta
zero. A tool that only counted would call this edit a wash. It
wasn't — one guardrail left, a different one arrived, and a third
was quietly demoted from an order to a suggestion.

And one prompt on its own:

```
$ promptdiff stats examples/new.md
====================================================================
PromptDiff — prompt profile
====================================================================
File:        examples/new.md
Fingerprint: 1d16043c13ec421a… (sha256 of normalized text)
Size:        719 chars · 122 words · 26 lines · 81 unique tokens
Sections:    5 — Support Agent Prompt — Acme Helpdesk, Role, Hard Rules, Tone, Output
Constraints: 4 (2 prohibitions, 2 requirements)
Format directives: 2 line(s)
Examples:    0 line(s) · URLs: 0
```

## Why

Prompt changes are the least reviewed changes in an agent system.
Code gets a diff view, a reviewer and a CI gate; the prompt — the
file that actually tells the agent what it may never do — gets edited
in a chat window and pasted in. PromptDiff gives that edit the same
treatment as code: a structured diff, a constraint ledger, and a
gate that fails when a guardrail weakens. Pair it with
[PromptRE](https://github.com/maticcretic-commits/promptre), which
takes a prompt apart; PromptDiff compares two of them.

## Part of AgentForge

PromptDiff is the eighth tool in the [AgentForge](https://github.com/maticcretic-commits)
trust layer for AI agents:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents — [github.com/maticcretic-commits/agentci](https://github.com/maticcretic-commits/agentci)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs — [github.com/maticcretic-commits/costledger](https://github.com/maticcretic-commits/costledger)
- **SchemaGuard** — validate agent outputs against their contracts — [github.com/maticcretic-commits/schemaguard](https://github.com/maticcretic-commits/schemaguard)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions (this repo)
- **TraceTape** — the canonical run tape — [github.com/maticcretic-commits/tracetape](https://github.com/maticcretic-commits/tracetape)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*; TraceTape keeps the whole run on *tape*.

## License

MIT
