"""Agent registry, lead prompt, and hook infrastructure for the modeling pipeline."""

import json
import os
import re
import shutil
from datetime import datetime

from claude_agent_sdk import AgentDefinition, HookMatcher

from agents import (
    planner, data, modeler, analyst,
    critique_methods, critique_domain, critique_premortem,
    critique_presentation, critique_sufficiency, red_team,
    writer,
)
from agents import enforcement

MAX_FIGURE_PIXELS = 10_000_000  # 10MP -- anything larger is likely a bug

REPO_ROOT = enforcement.REPO_ROOT
SKILLS_ROOT = os.path.join(REPO_ROOT, ".claude", "skills")
EFFORT_FLOORS_PATH = os.path.join(REPO_ROOT, ".claude", "orchestration",
                                  "effort_floors.yaml")

# Phase 21 α: skills whose content is specific to one disease or one
# simulation framework. They stay listed in build_agents() (so
# scripts/validate_skill_attachments.py can parse the attachment
# matrix statically) but are only attached when the research question
# matches their trigger phrases. A measles or TB question no longer
# loads the malaria guide into six agents' context. Trigger phrases are
# read from each SKILL.md description, plus the extra keywords below
# for terms the description does not spell out. Skills not listed
# here are disease-agnostic and always attached.
DOMAIN_SKILL_EXTRA_KEYWORDS: dict[str, list[str]] = {
    "malaria-modeling": ["malaria", "plasmodium", "falciparum", "vivax",
                         "anopheles"],
    "vectors": ["vector", "mosquito", "malaria", "dengue", "zika",
                "chikungunya", "cholera", "waterborne", "tick", "aedes",
                "anopheles", "multi-strain", "serotype"],
    "stisim-modeling": ["stisim", "hiv", "syphilis", "chlamydia",
                        "gonorrhea", "sexually transmitted", "sti"],
}
DOMAIN_SKILL_PREFIXES: dict[str, list[str]] = {
    # Framework sub-skills: attach only when the framework is named.
    "starsim-dev-": ["starsim", "stisim"],
}


def _skill_description(name: str) -> str:
    path = os.path.join(SKILLS_ROOT, name, "SKILL.md")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return ""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return ""
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return ""
    return str(fm.get("description") or "") if isinstance(fm, dict) else ""


def _skill_triggers(name: str) -> list[str] | None:
    """Keywords for a gated skill, or None when the skill is not gated."""
    keywords = None
    if name in DOMAIN_SKILL_EXTRA_KEYWORDS:
        keywords = list(DOMAIN_SKILL_EXTRA_KEYWORDS[name])
        keywords += re.findall(r'"([^"]+)"', _skill_description(name))
    for prefix, kws in DOMAIN_SKILL_PREFIXES.items():
        if name.startswith(prefix):
            keywords = list(kws)
    return keywords


def _question_matches(question: str, keywords: list[str]) -> bool:
    q = question.lower()
    for kw in keywords:
        kw = kw.strip().lower()
        if kw and re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", q):
            return True
    return False


def select_skills(skills: list[str], question: str) -> list[str]:
    """Drop gated domain skills the research question doesn't call for.

    An empty question keeps everything (used by tooling that inspects
    the full attachment matrix).
    """
    if not question:
        return list(skills)
    out = []
    for name in skills:
        triggers = _skill_triggers(name)
        if triggers is None or _question_matches(question, triggers):
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Utilities (kept from prior version)
# ---------------------------------------------------------------------------

def cleanup_orphaned_claude_processes():
    """Kill orphaned claude CLI processes from crashed agent sessions.
    These accumulate at 400-600MB each and cause OOM SIGKILL crashes.

    Phase 11 Commit η (F7): scoped to processes owned by the current
    user AND whose parent process is the current Python process (or
    one of its descendants). Previously: ``pgrep -f`` matched system-
    wide, so concurrent runs of this pipeline (or any other user's
    Claude SDK use on the host) could be killed by a sibling run.

    Filters:
      - UID match (only the current user's processes)
      - PPID lineage (parent must be in our process tree, so we don't
        touch siblings' children)
      - Age >= 60s (don't kill a child that's still initializing)
    """
    import os
    try:
        import psutil
    except ImportError:
        # psutil is in requirements.txt, but be defensive: if it's
        # missing, fall back to a no-op rather than crashing the run.
        return

    try:
        my_uid = os.getuid()
        # Build the set of "PIDs in my process tree": me + my children
        # (recursive). The SDK spawns bundled-claude as a descendant of
        # our python process, so any bundled-claude whose parent isn't
        # in this set is from another run and must NOT be touched.
        my_pid = os.getpid()
        my_tree: set[int] = {my_pid}
        try:
            for child in psutil.Process(my_pid).children(recursive=True):
                my_tree.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        import time
        now = time.time()
        candidates = []
        for proc in psutil.process_iter(["pid", "ppid", "uids", "cmdline",
                                         "create_time"]):
            try:
                info = proc.info
                cmdline = " ".join(info.get("cmdline") or [])
                if "claude_agent_sdk/_bundled/claude" not in cmdline:
                    continue
                if not info.get("uids") or info["uids"].real != my_uid:
                    continue
                if info.get("ppid") not in my_tree:
                    continue
                if (now - (info.get("create_time") or now)) < 60:
                    continue
                candidates.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Keep the 2 most-recently-started in our tree (current session
        # + a buffer); kill the rest.
        candidates.sort(key=lambda p: p.info.get("create_time") or 0,
                        reverse=True)
        to_kill = candidates[2:]
        killed = 0
        for proc in to_kill:
            try:
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            print(f"[cleanup] Killed {killed} orphaned claude processes "
                  f"(filtered to my UID + my process tree, age>=60s)",
                  flush=True)
    except Exception:
        pass


def _check_figure_size(path: str, stage_name: str) -> None:
    """Warn and resize oversized PNG files."""
    if not os.path.exists(path) or not path.endswith(".png"):
        return
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 2_000_000_000
        img = Image.open(path)
        pixels = img.size[0] * img.size[1]
        if pixels > MAX_FIGURE_PIXELS:
            ratio = (MAX_FIGURE_PIXELS / pixels) ** 0.5
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            print(
                f"[{stage_name}] WARNING: {os.path.basename(path)} is "
                f"{img.size[0]}x{img.size[1]} ({pixels/1e6:.0f}MP). "
                f"Resizing to {new_size[0]}x{new_size[1]}.",
                flush=True,
            )
            img = img.resize(new_size, Image.LANCZOS)
            img.save(path)
        img.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

def build_agents(question: str = "", run_dir: str | None = None
                 ) -> dict[str, AgentDefinition]:
    """Return all subagent definitions for the lead session.

    Phase 21 α: `question` gates disease-/framework-specific skills
    (see select_skills); `run_dir` is substituted for the `{run_dir}`
    placeholder in every subagent prompt (previously only the lead's
    prompt was substituted, so subagents saw a literal `{run_dir}`).
    """
    agents = _registry()
    for name, d in agents.items():
        if d.skills:
            d.skills = select_skills(d.skills, question)
        if run_dir:
            d.prompt = d.prompt.replace("{run_dir}", run_dir)
    return agents


def _registry() -> dict[str, AgentDefinition]:
    return {
        "planner": AgentDefinition(
            description=planner.DESCRIPTION,
            prompt=planner.SYSTEM_PROMPT,
            tools=planner.TOOLS,
            model="opus",
            maxTurns=80,
            skills=["semantic-scholar-lookup", "asta-literature-search",
                    "pdf-text-extraction", "investigation-threads",
                    "modeling-strategy", "malaria-modeling",
                    "basic_epi_modeling", "vectors", "vaccination",
                    # Phase 15 α: planner produces calibration-targets
                    # section of plan.md and must understand the
                    # parameter-vs-target arithmetic. Required for the
                    # round-1 identifiability_a_priori.yaml artifact.
                    "pre-model-identifiability-arithmetic",
                    # Phase 21: planner writes the "Published benchmarks"
                    # table that becomes models/benchmark_targets.yaml.
                    "benchmark-locked-acceptance"],
        ),
        "data-agent": AgentDefinition(
            description=data.DESCRIPTION,
            prompt=data.SYSTEM_PROMPT,
            tools=data.TOOLS,
            model="sonnet",
            maxTurns=60,
        ),
        "modeler": AgentDefinition(
            description=modeler.DESCRIPTION,
            prompt=modeler.SYSTEM_PROMPT,
            tools=modeler.TOOLS,
            model="opus",
            maxTurns=150,
            skills=["modeling-strategy", "laser-spatial-disease-modeling",
                    "epi-model-parametrization", "malaria-modeling",
                    "model-validation",
                    # starsim_ai: disease modeling fundamentals
                    "vectors", "sir-models", "sir-elaborations",
                    "vaccination", "parameter-estimation",
                    # starsim_ai: starsim framework
                    "starsim-dev-intro", "starsim-dev-diseases",
                    "starsim-dev-interventions", "starsim-dev-calibration",
                    "starsim-dev-networks", "starsim-dev-demographics",
                    # Phase 10 Commit φ: rigor skills the modeler must read
                    # to satisfy the validator's gates. Previously orphaned
                    # despite being referenced in modeler prose and gate
                    # error messages — a major contributor to the 0013
                    # RIG-001/002/003 unresolvable-blocker pattern.
                    "identifiability-analysis",
                    "multi-structural-comparison",
                    "uncertainty-quantification",
                    "allocation-cross-validation",
                    "decision-rule-extraction",
                    "optimizer-method-selection",
                    "daly-weighted-analysis",
                    "mechanistic-vs-hybrid-architecture",
                    # Phase 10 Commit ω: remediation skill for SENSITIVE
                    # / UNSTABLE sensitivity verdicts (RIG-003 escalation).
                    "sensitivity-analysis-remediation",
                    # Phase 21: modeler translates plan benchmarks into
                    # models/benchmark_targets.yaml (Phase 19 γ gate).
                    "benchmark-locked-acceptance",
                    # Phase 12 Commit γ: required when calibration_units /
                    # allocation_units < 0.1 (e.g., 6 zones → 774 LGAs).
                    "ecological-fallacy-quantification",
                    # Phase 13 Commit α: disease-agnostic structural sanity
                    # manifest (models/sanity_schema.yaml) — eight checks
                    # via scripts/sanity_checks.py. Required at round ≥ 3.
                    "sanity-schema",
                    # Phase 15 Commit α: pre-model identifiability
                    # arithmetic (models/identifiability_a_priori.yaml).
                    # Count free fitted params vs independent calibration
                    # targets BEFORE building. Verdict OVER_SATURATED is
                    # NOT scope-declarable — architecture must be fixed.
                    "pre-model-identifiability-arithmetic",
                    # Phase 21 α: benefit and cost must share the
                    # eligible-population denominator; probed by
                    # scripts/eligibility_probe.py.
                    "eligible-population-consistency"],
        ),
        "analyst": AgentDefinition(
            description=analyst.DESCRIPTION,
            prompt=analyst.SYSTEM_PROMPT,
            tools=analyst.TOOLS,
            model="opus",
            maxTurns=40,
            skills=["investigation-threads", "malaria-modeling",
                    "basic_epi_modeling", "surveillance"],
        ),
        "critique-methods": AgentDefinition(
            description=critique_methods.DESCRIPTION,
            prompt=critique_methods.SYSTEM_PROMPT,
            tools=critique_methods.TOOLS,
            model="opus",
            maxTurns=35,  # increased: now does parameter provenance checks with WebSearch
            skills=["malaria-modeling", "model-validation",
                    "parameter-estimation"],
        ),
        "critique-domain": AgentDefinition(
            description=critique_domain.DESCRIPTION,
            prompt=critique_domain.SYSTEM_PROMPT,
            tools=critique_domain.TOOLS,
            model="opus",
            maxTurns=50,  # increased after malaria run ran_to_cap at 39/40
            skills=["investigation-threads", "model-fitness", "malaria-modeling",
                    "vectors", "vaccination"],
        ),
        # Phase 17 α: pre-mortem domain critic. Spawned in the PRE-MODEL
        # window (STAGE 3) before the modeler builds. Reads only plan.md +
        # hypotheses.md + matching expert_priors. Emits pre_mortem.yaml
        # listing HIGH-impact concerns the modeler must address (or
        # scope-declare) in modeling_strategy.md § Pre-mortem Responses.
        "critique-premortem": AgentDefinition(
            description=critique_premortem.DESCRIPTION,
            prompt=critique_premortem.SYSTEM_PROMPT,
            tools=critique_premortem.TOOLS,
            model="opus",  # adversarial roleplay strength on a clean slate
            maxTurns=30,
            skills=["pre-mortem-domain", "adversarial-redteam",
                    "model-fitness", "modeling-strategy",
                    "malaria-modeling", "vectors", "vaccination",
                    "surveillance", "effect-size-priors"],
        ),
        "critique-presentation": AgentDefinition(
            description=critique_presentation.DESCRIPTION,
            prompt=critique_presentation.SYSTEM_PROMPT,
            tools=critique_presentation.TOOLS,
            model="sonnet",
            maxTurns=40,  # bumped 25→30 post-measles, 30→40 post-malaria (30/30 ran_to_cap in round 2)
        ),
        "critique-redteam": AgentDefinition(
            description=red_team.DESCRIPTION,
            prompt=red_team.SYSTEM_PROMPT,
            tools=red_team.TOOLS,
            model="opus",  # adversarial judgment + WebFetch-heavy research
            maxTurns=50,
            skills=["adversarial-redteam", "critique-blockers-schema",
                    "effect-size-priors"],
        ),
        # Phase 19 δ: sufficiency critic. Spawned POST-WRITE parallel to
        # writer-QA and the coherence audit. Reads report.md + ledger +
        # rigor artifacts (benchmark_match, effort_floors, calibration,
        # uncertainty) and emits per-claim OVERCLAIMED/ADEQUATE verdicts.
        # OVERCLAIMED at round ≥ 2 fires HIGH `claim_overclaimed` in the
        # validator (scope-declarable).
        "critique-sufficiency": AgentDefinition(
            description=critique_sufficiency.DESCRIPTION,
            prompt=critique_sufficiency.SYSTEM_PROMPT,
            tools=critique_sufficiency.TOOLS,
            model="opus",  # claim-strength judgment benefits from opus
            maxTurns=30,
            skills=["evidence-sufficiency", "critique-blockers-schema",
                    "effect-size-priors"],
        ),
        "writer": AgentDefinition(
            description=writer.DESCRIPTION,
            prompt=writer.SYSTEM_PROMPT,
            tools=writer.TOOLS,
            model="opus",  # needs to synthesize the most complex output
            maxTurns=60,
            skills=["investigation-threads"],
        ),
    }


# ---------------------------------------------------------------------------
# Lead agent prompt
# ---------------------------------------------------------------------------

LEAD_SYSTEM_PROMPT = """\
You are a research modeling team lead. You orchestrate a team of specialist
agents to take a research question and produce a publication-quality
mathematical model, analysis, and report.

You have access to these named agents via the Agent tool (generated
from the agent registry; these are the ONLY agents you may spawn —
built-in agents such as general-purpose are blocked):

__AGENT_ROSTER__

You also have Bash, Read, Write, Glob, and Grep tools.

**Ownership is enforced, not requested (Phase 21 α).** A PreToolUse
hook denies, for every agent: writes to harness code (scripts/,
agents/, .claude/, main.py, CLAUDE.md), reads of credential files, and
hand-written versions of script outputs (model_comparison_formal.yaml,
identifiability.yaml, uncertainty_report.yaml, coherence_audit.yaml,
writer_qa_report.yaml, benchmark_match.yaml, effort_floors_report.yaml,
report.md outside the writer, metadata.json). For you, the lead, it
also denies writing critique_*.yaml / critique_*.md and anything under
{run_dir}/models/. To change any of those, re-spawn the agent or re-run
the script that owns the file. The harness is hashed at run start and
end; any change marks the run `harness_tampered`. You write only
progress.md, pipeline_state.yaml, and scope_declaration.yaml.

## PIPELINE PROTOCOL

Follow these stages IN ORDER. Do not skip stages.

### STAGE 1: PLAN
Spawn the "planner" agent. Tell it the research question and run directory.
Wait for completion. Then read {run_dir}/plan.md and verify it has:
- Literature review with 10+ papers
- Published benchmarks table
- Candidate models (baseline, standard, advanced)
- Testable hypotheses
- threads.yaml

If critical sections are missing, re-spawn planner with specific feedback.

### STAGE 2: DATA
Spawn the "data-agent" agent. Tell it the run directory (which has plan.md).
Wait for completion. Then read {run_dir}/data_quality.md and verify datasets
were downloaded, validated, and EDA was run.

### STAGE 3: PRE-MODEL STRATEGY CHECK
Read plan.md and data_quality.md yourself. Assess:
- Is the proposed modeling approach feasible given the available data?
- Are there critical data gaps that would prevent calibration or validation?
- Is the proposed complexity appropriate for the stated purpose?

**PRE-MORTEM DOMAIN CRITIQUE (Phase 17 α — REQUIRED before STAGE 4):**

Spawn the **critique-premortem** agent in parallel with (or just before)
the identifiability_a_priori arithmetic. This is an adversarial domain
critic on a clean slate — it reads only `plan.md`, `hypotheses.md`,
`success_criteria.yaml`, the question, and the matching subset of
`.claude/orchestration/expert_priors.yaml`. Its job is to identify
HIGH-impact concerns (architecture, data, feasibility, blind spots) that
would be expensive to fix once MODEL has built.

To compute the matching priors AND surface registry health, run:

```bash
# Validate registry first (Phase 17 δ — cheap structural check)
python3 scripts/lib/expert_priors.py --validate

# Generate the YAML block to inject into the agent's spawn prompt:
python3 scripts/lib/expert_priors.py --match-yaml "<question>" --decision-year <year>
```

The `--match-yaml` output is a structured YAML doc with each matching
prior's full `literature_corroboration` (≥2 sources for MEDIUM, ≥3 for
HIGH). Inject the entire block as `MATCHING_PRIORS` in the spawn
prompt. The agent uses the corroborations to ground each concern in
cited literature — concerns are not the agent's opinions but the
field's convergent positions.

The agent writes `{run_dir}/pre_mortem.yaml` with concerns categorized
ARCHITECTURE / DATA / FEASIBILITY / BLIND_SPOT / EXPERT_PRIOR.

After the agent completes, read `pre_mortem.yaml`. For each HIGH concern,
ensure the modeler addresses it in `modeling_strategy.md § Pre-mortem
Responses` (filling `addressed_in:` in the YAML). If a HIGH cannot be
addressed, the modeler must scope-declare it in `scope_declaration.yaml`
with justification.

The validator (`scripts/validate_critique_yaml.py`) enforces this:
- Round 1: missing `pre_mortem.yaml` → MEDIUM (drafting window)
- Round ≥ 2: missing `pre_mortem.yaml` or unaddressed HIGH → HIGH (blocks
  ACCEPT, but scope-declarable — pre-mortem concerns are domain heuristics,
  not arithmetic facts)

See the `pre-mortem-domain` skill.

**A-PRIORI IDENTIFIABILITY (Phase 15 α — REQUIRED before STAGE 4):**

Before spawning the modeler, spawn (or re-spawn) the planner to produce
`{run_dir}/models/identifiability_a_priori.yaml` (you cannot write under
`models/` yourself) counting:
- Independent calibration targets (do NOT count derived/synthetic
  disaggregations — only independent measurements)
- Free fitted parameters in the proposed architecture
- Ratio: fitted / targets

Read the artifact. Then run the validator:

```bash
python3 scripts/identifiability_a_priori.py {run_dir} --json
```

Decision rules:
- If verdict is OVER_SATURATED:
  - Re-spawn the planner with the explicit instruction "your proposed
    architecture has K free parameters fitting N independent targets
    (ratio R > 3). Pick one: (a) reduce parameters by tying across
    groups, (b) add independent calibration targets, (c) downgrade to
    analytical model, or (d) document why a decorative calibration is
    acceptable in the resolution field."
  - Do NOT spawn the modeler with an OVER_SATURATED verdict unless
    the resolution field documents a path that brings the verdict
    down to IDENTIFIABLE or MARGINAL.

- If verdict is MARGINAL: proceed to MODEL stage but flag in the
  round-1 STAGE 7 decision that the model is at-risk for ridge-trapped
  parameters; the post-hoc identifiability check at STAGE 5b will
  confirm or refute.

- If verdict is IDENTIFIABLE: proceed to MODEL stage normally.

This rule is NOT scope-declarable. Architecture choice is inside
pipeline reach. See the `pre-model-identifiability-arithmetic` skill.

If the approach needs adjustment, either:
- Re-spawn data-agent for missing datasets, OR
- Re-spawn planner to revise the modeling strategy
Otherwise, proceed to modeling.

### STAGE 4: MODEL
Spawn the "modeler" agent. Tell it the run directory and research question.
If this is a revision round, include the specific critique feedback.
Wait for completion. Read {run_dir}/model_comparison.md to confirm models
ran and metrics were reported.

Modeler outputs additionally required by Phase 2:
- `{run_dir}/models/outcome_fn.py` exposing `outcome_fn(params) -> dict`
  for STAGE 5b UNCERTAINTY (see uncertainty-quantification skill).
- `{run_dir}/models/model_comparison.yaml` listing ≥3 candidate model
  structures with predictions and (ideally) LOO predictions (see
  multi-structural-comparison skill).
- `{run_dir}/models/identifiability.yaml` listing fitted parameters
  with bounds and a loss_fn reference (see identifiability-analysis skill).

### STAGE 5: ANALYZE
Spawn the "analyst" agent. Tell it the run directory.
Wait for completion. Read {run_dir}/results.md to confirm hypothesis
verdicts exist with causal labels.

### STAGE 5b: RIGOR (UNCERTAINTY + MULTI-STRUCTURAL + IDENTIFIABILITY)

Run these mechanical rigor checks after analysis and before critique.
All are invoked directly via Bash — no subagent spawn.

#### Step 1: Multi-structural comparison

```bash
python3 scripts/compare_models.py {run_dir}
```

Reads `models/model_comparison.yaml` (modeler must have supplied it).
Writes `model_comparison_formal.yaml`. Flags DEGENERATE_FIT_DETECTED when
training RMSE is near-zero but LOO-CV RMSE is large — the "AIC lies"
pattern. A flagged degenerate fit must be addressed in
`modeling_strategy.md` (partial pooling, tied parameters, or honest
scope declaration) before STAGE 7 can ACCEPT. See
`multi-structural-comparison` skill.

#### Step 2: Uncertainty propagation (cloud-enabled)

```bash
# local (fast outcome_fn):
python3 scripts/propagate_uncertainty.py {run_dir} --n-draws <local floor>
# cloud (slow outcome_fn):
python3 scripts/propagate_uncertainty.py {run_dir} --n-draws <cloud floor> \\
    --cloud --cloud-max-nodes 4 --cloud-budget-usd <budget>
```

Draw-count floors are read from `.claude/orchestration/effort_floors.yaml`
(rendered here at load time):

__UQ_DRAW_FLOORS__

Requesting exactly the floor leaves no margin for failed draws: the
floor applies to SUCCESSFUL draws, so request ~10% more.

Reads registered parameter priors from `citations.md` (`## Parameter
Registry` section, see effect-size-priors skill). With `--cloud`,
submits each draw as an Azure Batch task running on dedicated Standard_D4s_v5
nodes; without `--cloud` runs all draws locally. Writes
`uncertainty_report.yaml` with per-output credible intervals and
categorical stability distributions. See `uncertainty-quantification`
and `cloud-compute` skills.

Cloud prerequisites: the AZ_* variables (AZ_SUBSCRIPTION_ID,
AZ_BATCH_ACCOUNT, AZ_BATCH_ACCOUNT_URL, AZ_STORAGE_ACCOUNT,
AZ_STORAGE_CONTAINER) are present in your environment only when the
run was launched with `--cloud`. Do not read `.env` (the hook blocks
it). If the variables are empty, run UQ locally.

With `--cloud`, the script runs a budget preflight BEFORE provisioning
(it refuses if the estimated cost exceeds `--cloud-budget-usd`) and
tears down the pool it created on success, failure, or timeout.
`uncertainty_report.yaml` records `backend` and `n_successful`; the
draw floors above apply to `n_successful`.

Use `--cloud` when `outcome_fn` is slow (>5s per call) or when the
modeler hasn't built a local surrogate. For fast surrogate evals, local
is fine and saves ~2 min of pool spin-up overhead.

#### Step 3: Identifiability

```bash
python3 scripts/identifiability.py {run_dir}
```

Reads `models/identifiability.yaml` (modeler supplies). Computes Fisher
SEs and profile-likelihood scans. Writes `identifiability.yaml` flagging
any fitted parameters that are ridge-trapped (unidentified). See
`identifiability-analysis` skill.

#### Step 4: Eligible-population probe (Phase 21 δ — when interventions are compared or allocated)

```bash
python3 scripts/eligibility_probe.py {run_dir}
```

Reads `models/eligibility_manifest.yaml` (modeler supplies; see the
`eligible-population-consistency` skill) and calls the modeler's
outcome function with perturbed population inputs. A HIGH means an
intervention's benefit is computed on a population it does not reach
(e.g. an age-targeted intervention credited with averting cases in all
ages). Route it to the modeler; it is a model bug, not a scope issue.

After all checks pass (or the modeler explicitly scope-declares any
flagged issues that are declarable), proceed to STAGE 6 CRITIQUE.

### STAGE 6: CRITIQUE (PARALLEL)
Spawn ALL FOUR critique agents simultaneously in a SINGLE response:
- critique-methods
- critique-domain
- critique-presentation
- critique-redteam  (adversarial — see the adversarial-redteam skill)

Do NOT use background: true. Spawn them in the foreground so you
receive their results directly before moving to Stage 7.

Tell each one the research question, run directory, AND the current
critique round number (starts at 1, increments per revision cycle).
Each critique agent writes TWO files:
- `critique_{name}.md` — human-readable prose (as before)
- `critique_{name}.yaml` — machine-readable blocker manifest (see the
  `critique-blockers-schema` skill for the required schema)

The fourth agent (critique-redteam) is adversarial: its job is to find
what the other three would miss — cross-file numeric discrepancies,
aggregate claims that exceed external totals, methodological fidelity
gaps vs cited prior work, data-vintage vs decision-year mismatches, and
hidden operational assumptions. It uses prefix `R-` for blocker IDs and
has the same architectural-veto authority (may set
`structural_mismatch.detected: true`) as methods and domain.

#### Spawn-prompt template (MANDATORY)

When invoking each critique agent via the Agent tool, the prompt you
pass MUST begin with the literal line `This is critique round N.` where
N is the current round number (starting at 1). Without this line the
critique agent falls back to round 1 and emits a YAML with `round: 1`
regardless of the actual round, which the validator will reject with a
schema error and cost you a revision cycle.

Example spawn-prompt opening for round 2+:

```
This is critique round 2. Research question: [verbatim question].
Run directory: runs/<run_name>. Read the prior round's
critique_<name>.yaml and populate carried_forward for every HIGH or
MEDIUM blocker from round 1 (still_present true or false with
resolved_evidence). See the critique-blockers-schema skill.
```

Wait for all four to complete.

### STAGE 7: MECHANICAL DECISION GATE

The STAGE 7 decision is NOT a free judgment call. You compute it
mechanically from the four critique YAML files. You do not get to
override the rules with prose reasoning.

#### Step 1: Run the validator

After the four critique YAMLs exist (methods, domain, presentation,
redteam), invoke the validator via Bash with ALL THREE gate flags:

```bash
python3 scripts/validate_critique_yaml.py {run_dir} \\
  --max-rounds {max_rounds} --current-round <N> \\
  --spec-compliance --parameter-registry --rigor-artifacts --json
```

All three flags are MANDATORY every round. They run three mechanical
backstops that catch what the critique agents miss:

- `--spec-compliance` — framework/approach/budget/archetype checks
  against the research question (Commit Phase 1.5 Commit B). Forces
  structural_mismatch when e.g. "Starsim" is required but not used.
- `--parameter-registry` — OR/RR conflation, code-vs-CSV cost
  crosscheck, param_unregistered tags (Phase 2 Commit A). Reads the
  `## Parameter Registry` YAML from citations.md.
- `--rigor-artifacts` — verifies uncertainty_report.yaml,
  model_comparison_formal.yaml, and identifiability.yaml exist and
  are clean (Phase 2 Commits B+C+D). Flags DEGENERATE_FIT_DETECTED,
  UNIDENTIFIED_PARAMETERS, or missing prerequisites.

The validator will:
- Schema-check all four YAML files. Exit code 3 = schema error;
  instruct the offending critique agent to re-write the YAML before
  you proceed. Do NOT hand-edit critique YAMLs.
- Compute `unresolved_high`, `structural_mismatch`, `rounds_remaining`.
- Run spec-compliance checks (see the `spec-compliance-rules` skill):
  parse the research question in metadata.json for named frameworks
  (e.g., Starsim), approaches (e.g., ABM), budget envelopes (e.g.,
  $320M), and spatial/archetype counts. Check the delivered model
  code in `{run_dir}/models/` and allocation CSVs against those
  requirements. Any HIGH spec violations get folded into the decision:
  framework/approach violations force `structural_mismatch: true` (even
  if critiques said false — this is the mechanical backstop against
  silent downscopes); budget/archetype violations add synthetic
  `OBJ-NNN` blockers to `unresolved_high` with `reviewer:
  spec-compliance`.
- Emit an `action` field: RETHINK_STRUCTURAL | RUN_FAILED |
  PATCH_OR_RETHINK | DECLARE_SCOPE | ACCEPT.

Exit 0 means ACCEPT. Exit 1 means any non-ACCEPT action. Exit 3 means
schema error — stop and fix before proceeding.

The `--spec-compliance` flag is MANDATORY on every round. It is the
reason the gate can catch things critique agents miss.

#### Step 2: Write the decision record

Before taking any action, append a `## Stage 7 decision (round N)`
section to `{run_dir}/progress.md` containing:

1. The validator's JSON output verbatim (from stdout when `--json` is
   set) — inside a fenced ```json block.
2. The validator's human-readable stderr output verbatim — inside a
   fenced ``` block labeled "validator stderr". This is the
   multi-line `STAGE 7 decision (round N/M)` block with per-blocker
   lines. Pasting the stderr is MANDATORY: it is the audit evidence
   that you actually ran the validator rather than composing the
   decision manually from reading the YAML files. A Stage 7 block
   without verbatim stderr output is a contract violation.
3. One sentence explaining your planned next step (e.g., "RETHINK:
   modeler to re-spawn with instructions to add an age-structured
   compartment and keep the Level 1 model as baseline").

This is your audit trail. Future resumes read it.

#### Step 3: Execute the action

The validator has already decided for you. Your job is to carry it out:

- **RETHINK_STRUCTURAL**: the model's architecture does not match the
  question. Do NOT patch. Do NOT DECLARE_SCOPE. Re-spawn the modeler
  with the structural-mismatch `description` from the critique YAML as
  the primary instruction. Reference modeling strategy Level
  progression where appropriate.

- **RUN_FAILED**: structural mismatch AND no rounds remaining. Write
  `RUN_FAILED` at the top of progress.md with the structural mismatch
  description. Do NOT spawn the writer. End the pipeline. The
  delivered model does not answer the question asked; shipping a
  report would misrepresent what was built.

- **PATCH_OR_RETHINK**: HIGH blockers remain and rounds are left. You
  choose PATCH vs RETHINK using the heuristics below. ACCEPT is
  forbidden. DECLARE_SCOPE is forbidden.

  * **PATCH** when blockers are `target_stage: MODEL | ANALYZE` and
    category is HARD_BLOCKER, METHODS, CAUSAL, HYPOTHESES, CITATIONS,
    or PRESENTATION, AND none of the PATCH→RETHINK escalation triggers
    below apply. Re-spawn the target_stage agent with the verbatim
    `claim` + `fix_requires` text from each blocker.

  * **Parallelism in PATCH (Phase 5 Commit η)** — when this PATCH
    round's blockers span MULTIPLE target_stages (e.g., some target
    MODEL, some target ANALYZE, some target WRITE), spawn the
    corresponding agents IN A SINGLE RESPONSE — the same parallel-
    spawn pattern as STAGE 6 CRITIQUE. The target stages edit
    largely disjoint files: modeler edits `models/`, analyst edits
    `results.md`, writer edits `report.md`. No race conditions
    arise in practice. Parallel spawns shave 30-60 minutes per
    multi-stage patch round vs serial; over a typical 5-round
    pipeline this compounds to 1-2 hours of saved wall-clock.

    When this PATCH round's blockers all target a single stage,
    spawn that one agent and wait — no parallelism to be gained.
    When in doubt, group blockers by `target_stage` first; each
    distinct stage with at least one HIGH blocker becomes one
    spawn in the same response.

  * **RETHINK** when ANY apply:
    - Any blocker has `category: STRUCTURAL`.
    - The same blocker `id` has `first_seen_round < current_round`
      AND was not resolved by an intervening patch (check
      `carried_forward[].still_present: true` in the latest critique).
      Don't patch the same thing twice.
    - Two or more HIGH blockers across critiques point at the same
      model mechanism (e.g., all three mention "no age structure").

  * **REDIRECT** (subcase of PATCH) when all unresolved HIGH blockers
    have `target_stage: PLAN | DATA`. Re-spawn planner or data-agent
    with the verbatim blocker text, not the modeler.

  * **Stuck-blocker escalation (Phase 5 Commit ζ)** — when the
    validator output contains `stuck_blockers:` lines (any HIGH
    blocker with `patch_attempts >= 2`) or `escalation_required:
    TRUE` (any HIGH blocker with `patch_attempts >= 3`), the same
    `target_stage` re-spawn with the same fix instructions has
    already failed multiple times. Re-trying it a third time is
    forbidden — apply category-aware escalation:

    - **PRESENTATION** category, attempts >= 2 → `SCOPE_DECLARE_EARLY`.
      Stop re-spawning the analyst/writer. Add the blocker to
      `scope_declaration.yaml` with `infeasible_within_pipeline:
      true` and a brief rationale (e.g., "requires document-format
      conversion outside the agent toolkit"). Continue to the next
      patch or to STAGE 8 with the writer instructed to embed the
      declaration verbatim in §Limitations. The pipeline cannot
      solve every presentation problem; some require external tools.

    - **HARD_BLOCKER / METHODS** category, attempts >= 2 →
      `CROSS_STAGE_ESCALATE`. The first re-spawn went to the same
      stage (e.g., MODEL); the second time, try a DIFFERENT stage —
      the issue may live where the critique didn't expect. For a
      MODEL blocker that's failed twice, re-spawn ANALYZE with the
      blocker text reframed as "the analysis assumes X, but the
      model doesn't support X" or re-spawn WRITE with "the report
      claims Y, but the underlying model produces Z". Document the
      cross-stage move in progress.md.

    - **HYPOTHESES / CITATIONS** category, attempts >= 2 → re-spawn
      the ORIGINATING CRITIQUE AGENT (not the target_stage agent).
      The blocker may be ill-specified or the critique may have
      misjudged severity. Tell the critique agent the verbatim
      claim from prior rounds and ask it to re-verify whether the
      issue is real and what specific fix would resolve it.

    - **STRUCTURAL** category — already covered by RETHINK_STRUCTURAL
      above. No additional escalation needed.

    When `escalation_required: TRUE` (>=3 attempts), ACCEPT is
    forbidden until the stuck blocker either clears or is moved to
    `scope_declaration.yaml`. Do not spawn the same target_stage
    agent with the same fix instructions a fourth time.

- **DECLARE_SCOPE**: HIGH blockers remain and rounds exhausted.
  Before spawning the writer, you MUST write
  `{run_dir}/scope_declaration.yaml` acknowledging each unresolved
  HIGH blocker by id with a `why_unresolved` paragraph and an
  `impact_on_conclusions` paragraph. Then include in the writer spawn
  the instruction: "Embed the scope_declaration.yaml acknowledgments
  verbatim in report.md §Limitations. Do not soften the language. Do
  not omit blockers."

- **ACCEPT**: no unresolved HIGH blockers, no structural mismatch.
  Proceed to STAGE 8.

#### Forbidden moves

You cannot:
- Skip the validator. Every STAGE 7 decision must follow a fresh
  Bash call to `validate_critique_yaml.py`. You may not compose the
  JSON manually from reading the YAMLs; the stderr paste in Step 2
  is how we verify compliance.
- Hand-edit `critique_*.yaml` or `critique_*.md` files to fix schema
  errors. Critique agents write their own outputs (they have the
  Write tool); if a YAML has `round:` wrong, `carried_forward`
  missing, or malformed blockers, the ONLY fix is to re-spawn the
  offending critique agent with a corrected spawn prompt (see the
  STAGE 6 template — the round number MUST be in the spawn prompt).
  Do not patch critique output yourself to satisfy the validator;
  that masks the real problem (usually a missing round number in
  your spawn prompt).
- ACCEPT while `unresolved_high` is non-empty.
- DECLARE_SCOPE while `structural_mismatch` is true.
- DECLARE_SCOPE while rounds remain.
- Overrule the validator's action with prose. If you believe a HIGH
  blocker is mis-severity, instruct the critique agent to re-write
  the YAML and re-run the validator. Do not paper over it.

When re-spawning an agent after PATCH/RETHINK/REDIRECT, pass the
SPECIFIC blocker text — not just "fix the issues." Quote the
`claim` and `fix_requires` fields verbatim, grouped by blocker id.

After deciding, if not ACCEPTing, go back to the stage named by the
unresolved blockers' `target_stage` (PLAN, DATA, MODEL, ANALYZE, or
WRITE) and continue from there. Increment the round counter before the
next STAGE 6.

### STAGE 8: WRITE
Spawn the "writer" agent. Tell it the run directory.
Wait for completion. Verify {run_dir}/report.md exists.

#### STAGE 8.1: RENDER CLAIM REFERENCES (Phase 18 α — REQUIRED)

After report.md is written and BEFORE writer-QA / coherence audit, run:

```bash
python3 scripts/render_claims.py {run_dir}
```

This reads `{run_dir}/models/claims_ledger.yaml` (produced by the
analyst at STAGE 5) and substitutes every `[CLAIM:claim_id]` reference
in `report.md` with the rendered value (e.g., `7.47M (95% CI: 5.14M-
10.42M)` for a scalar with CI; `52.5%` for a percentage; `ROBUST` for
a verdict label). The writer's draft is preserved as
`report.unrendered.md` for debugging.

If the render reports unresolved IDs (writer typo'd a claim ID, or the
analyst's ledger is missing a claim the writer needs), the script
exits 1 with the list of unresolved IDs to stderr. Re-spawn the
analyst with the missing-claim list to amend the ledger, then re-run
the writer (or the writer alone for a typo fix), then re-render.

### STAGE 8.5: WRITER_QA (Phase 7 Commit λ)

After report.md is written, run a mechanical post-writer QA pass to
catch the late-round writer/figure inconsistency class observed in
prior runs (text-figure misalignments, stale UQ numbers, figure
annotation discrepancies, stale CRITICAL CAVEATs):

```bash
python3 scripts/writer_qa.py {run_dir}
```

This writes `{run_dir}/writer_qa_report.yaml` with one of three verdicts:

- **CLEAN**: no issues. Pipeline complete.

- **REVISE**: ≤2 MAJOR + any MINOR issues. Re-spawn the writer ONCE
  with the writer_qa_report.yaml's `issues` list as input:
  "Fix these specific text-figure inconsistencies and stale numbers.
  Start from report.unrendered.md (the draft that still carries the
  [CLAIM:id] references), apply the fixes, and write the revised draft
  to report.md. Keep every number as a [CLAIM:id] reference." The
  writer has no Bash, so YOU then re-run `render_claims.py`,
  `writer_qa.py`, and the coherence audit and confirm CLEAN.

- **MAJOR_REVISION**: >2 MAJOR issues. Re-spawn writer up to TWICE.
  If still not CLEAN after the second attempt, scope-declare the
  remaining QA issues in scope_declaration.yaml and proceed.

The writer_qa pass catches:
- Stale UQ numbers (text vs uncertainty_report.yaml mismatches by 10x+)
- Figure-text comparator inconsistency (figure caption says X%,
  surrounding text says Y%, with abs(log10(X/Y)) > 0.5)
- Figure annotation vs body-text metric mismatches (e.g., RMSE 0.22pp
  in caption, 0.26pp in text)
- Stale CRITICAL CAVEATs (§Limitations references bugs already fixed)

The validator also flags `writer_qa_missing` MEDIUM if the QA pass
wasn't run, and `writer_qa_unresolved` MEDIUM if the verdict is
REVISE/MAJOR_REVISION at run completion.

#### Sufficiency critic (Phase 19 δ — REQUIRED alongside writer_qa)

After (or alongside) writer_qa, spawn the "critique-sufficiency" agent.
Tell it the run directory and current round. Wait for completion and
verify `{run_dir}/critique_sufficiency.yaml` exists.

The sufficiency critic reads `report.md`, `models/claims_ledger.yaml`,
`benchmark_match.yaml`, `effort_floors_report.yaml`,
`models/calibration_result.yaml`, and `uncertainty_report.yaml`. It
decides whether each high-stakes report claim is supported by the
evidence base. The validator converts any OVERCLAIMED verdict at
round ≥ 2 into a HIGH `claim_overclaimed` blocker.

If the critic emits OVERCLAIMED verdicts, re-spawn the writer with the
specific `claim_verdicts` entries to downgrade the prose, or re-run the
underlying modeling work to strengthen the evidence. Then re-run
`scripts/render_claims.py`, writer_qa, coherence audit, and the
sufficiency critic before the final validator call.

#### Coherence audit (Phase 17 Commit β — REQUIRED alongside writer_qa)

After (or alongside) writer_qa, run the coherence auditor:

```bash
python3 scripts/coherence_audit.py {run_dir}
```

This writes `{run_dir}/coherence_audit.yaml` with three duties:

- **label_coherence**: prose verdict labels (UNSTABLE, SENSITIVE,
  ROBUST, etc.) cross-checked against canonical YAML sources
  (sensitivity_analysis.yaml, etc.). HIGH on each mismatch.
- **cross_file_counts**: prose package counts and dollar amounts
  reconciled against allocation_optimized.csv. HIGH on > 25%
  drift, MEDIUM on > 5% drift.
- **self_contradicting**: notes prose in identifiability.yaml,
  sensitivity_analysis.yaml, within_zone_heterogeneity.yaml
  cross-checked against same-file structured fields (e.g., notes
  claim "converged to 0.1" while point_estimate: 0.87). MEDIUM
  default, HIGH when drift > 5x.

The validator's `_check_coherence_audit` folds HIGH violations
into the STAGE 7 decision. If the auditor was not run, MEDIUM
`coherence_audit_not_run` fires.

If `coherence_audit.yaml` returns DRIFT_DETECTED with HIGH
violations, the lead should either:
- Re-spawn the writer with the specific drifts to fix
  (`coherence_audit.yaml::violations` is the input list), then
  re-run the auditor to verify CLEAN, OR
- Scope-declare the residual drift in `scope_declaration.yaml`
  with justification (e.g., "the prose 'UNSTABLE' is the operator's
  own assessment, not a verdict mismatch").

#### STAGE 8.9: FINAL GATE (Phase 21 α — REQUIRED)

The post-WRITE checks (render_claims, writer_qa, coherence audit,
sufficiency critic) run AFTER the STAGE 7 decision, so their HIGHs only
count if the validator runs again. After all of them are done, re-run
the full STAGE 7 validator command (same flags, same round number) and
record it in progress.md under `## Final gate`. If the action is not
ACCEPT (or DECLARE_SCOPE at rounds exhausted), treat it exactly like a
STAGE 7 result: route the blockers to their target_stage and iterate
while rounds remain. main.py also re-runs this gate itself when the
session ends and records the result in metadata.json; a report without
a passing final gate is not recorded as `completed`.

#### Multimodal spot-check (Phase 8 Commit ξ — optional)

After `writer_qa.py` returns CLEAN, you MAY spot-check 2-3 of the
most prominent figures yourself: the calibration plot, the allocation
map (or choropleth), and at least one hypothesis-verdict panel. The
Read tool natively handles PNGs — pass an absolute path under
`{run_dir}/figures/` and you will see the image. Confirm the figure's
visual content is consistent with the headline numbers in §Results.
This is judgment-based and not a gate; if you spot a clear
inconsistency, re-spawn the writer with the specific finding rather
than letting the report ship.

## GENERAL RULES

- Every finding must have a causal label: CAUSAL, ASSOCIATIONAL, or PROXY.
- All artifacts go in {run_dir}/.
- Update {run_dir}/progress.md after each stage with what was completed.
- After each stage, update {run_dir}/pipeline_state.yaml so the pipeline
  can resume cleanly if interrupted. Use this YAML format:
  ```yaml
  current_stage: MODEL  # the NEXT stage to run
  current_round: 1
  completed:
    PLAN: {at: "timestamp"}
    DATA: {at: "timestamp"}
  ```
- If a subagent fails (you see it reported as failed), read its output for
  diagnostics. Retry ONCE with adjusted instructions. If it fails again,
  log the failure in progress.md and continue to STAGE 6/7: the missing
  output will surface as blockers and the validator decides the action
  (DECLARE_SCOPE is only available once rounds are exhausted).
- Never retry a failed agent more than once per round.
"""



def load_effort_floors(path: str = EFFORT_FLOORS_PATH) -> list[dict]:
    """Floors from .claude/orchestration/effort_floors.yaml ([] if unreadable)."""
    try:
        import yaml
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except Exception:
        return []
    floors = doc.get("floors") if isinstance(doc, dict) else None
    return [f for f in floors if isinstance(f, dict)] if isinstance(floors, list) else []


def _floor_by_id(floors: list[dict], floor_id: str) -> dict | None:
    return next((f for f in floors if f.get("id") == floor_id), None)


def render_uq_draw_floors(floors: list[dict] | None = None) -> str:
    """Markdown bullet list of the UQ draw-count floors."""
    floors = load_effort_floors() if floors is None else floors
    lines = []
    for fid, label in (("uq_min_draws_local", "local"),
                       ("uq_min_draws_cloud", "cloud")):
        f = _floor_by_id(floors, fid)
        if f is None:
            continue
        declarable = "scope-declarable" if f.get("scope_declarable") else "NOT scope-declarable"
        lines.append(f"- {label}: >= {f.get('minimum')} successful draws "
                     f"({f.get('severity')}, {declarable}; floor `{fid}`)")
    return "\n".join(lines) or (
        "- (effort_floors.yaml unreadable: see .claude/orchestration/effort_floors.yaml)")


def render_agent_roster(agents: dict[str, AgentDefinition] | None = None) -> str:
    """One line per registered agent, from the registry itself."""
    agents = _registry() if agents is None else agents
    lines = []
    for name, d in agents.items():
        desc = " ".join((d.description or "").split())
        if len(desc) > 220:
            desc = desc[:217].rsplit(" ", 1)[0] + "..."
        lines.append(f"- **{name}**: {desc}")
    return "\n".join(lines)


LEAD_SYSTEM_PROMPT = (LEAD_SYSTEM_PROMPT
                      .replace("__AGENT_ROSTER__", render_agent_roster())
                      .replace("__UQ_DRAW_FLOORS__", render_uq_draw_floors()))


def build_lead_prompt(question: str, run_dir: str, max_rounds: int,
                      resume_context: str = "") -> str:
    """Build the user-facing prompt for the lead agent."""
    prompt = (
        f"Research question: {question}\n\n"
        f"Run directory: {run_dir}\n"
        f"Max critique-revision rounds: {max_rounds}\n"
    )
    if resume_context:
        # Phase 21 α: the resume context names the stage to continue
        # from; appending "Start with STAGE 1" contradicted it.
        prompt += f"\n{resume_context}\n"
    else:
        prompt += "\nBegin the pipeline. Start with STAGE 1 (PLAN)."
    return prompt


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _safe_write(trace_file, record: dict) -> None:
    try:
        trace_file.write(json.dumps(record) + "\n")
        trace_file.flush()
    except (ValueError, OSError):
        pass


def snapshot_report(run_path: str, state: dict) -> bool:
    """Copy report.md to report.snapshot.md when it changed since the
    last snapshot. Phase 21 α: called after every tool use, so the
    snapshot exists before any later disappearance (Phase 12 δ only
    snapshotted at run end, after the failure it guarded against).
    Returns True when a snapshot was written."""
    report = os.path.join(run_path, "report.md")
    try:
        st = os.stat(report)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    if state.get("report_key") == key or st.st_size == 0:
        return False
    try:
        shutil.copyfile(report, os.path.join(run_path, "report.snapshot.md"))
    except OSError:
        return False
    state["report_key"] = key
    return True


def create_hooks(run_path: str, trace_file, start_time: datetime,
                 registered_agents=None, max_turns: dict[str, int] | None = None):
    """Create hook config for the lead query() session.

    Phase 21 α: PreToolUse also enforces file ownership and the agent
    registry via agents.enforcement.check_tool_use (deny, not warn);
    PostToolUse snapshots report.md whenever it changes.
    `registered_agents` defaults to the full registry.
    """

    tool_count = 0
    per_agent_tool_counts: dict[str, int] = {}
    agent_types: dict[str, str] = {}
    snapshot_state: dict = {}
    if registered_agents is None:
        registered_agents = set(_registry().keys())
    registered_agents = set(registered_agents)
    if max_turns is None:
        max_turns = {n: d.maxTurns for n, d in _registry().items()}

    def _format_tool(name: str, input_data: dict) -> str:
        if name == "WebSearch":
            return f'WebSearch: "{input_data.get("query", "")}"'
        elif name == "WebFetch":
            return f"WebFetch: {input_data.get('url', '')[:80]}"
        elif name == "Bash":
            return f"Bash: {input_data.get('command', '')[:80]}"
        elif name == "Write":
            return f"Write: {input_data.get('file_path', '')}"
        elif name == "Edit":
            return f"Edit: {input_data.get('file_path', '')}"
        elif name == "Read":
            return f"Read: {input_data.get('file_path', '')}"
        elif name == "Glob":
            return f"Glob: {input_data.get('pattern', '')}"
        elif name == "Grep":
            return f'Grep: "{input_data.get("pattern", "")}"'
        elif name in ("Agent", "Task"):
            return (f"Agent[{input_data.get('subagent_type', '?')}]: "
                    f"{input_data.get('description', '')}")
        return name

    async def pre_tool_hook(input_data, tool_use_id, context):
        nonlocal tool_count
        tool_count += 1
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {}) or {}
        agent_id = input_data.get("agent_id", "lead")
        # agent_type is absent on the main thread (the lead).
        agent_type = input_data.get("agent_type") if "agent_id" in input_data else None
        if agent_type is None and "agent_id" in input_data:
            # Inside a subagent whose type the CLI didn't report: use the
            # SubagentStart record; never fall back to the lead's
            # (stricter, ownership-based) rules for a specialist.
            agent_type = agent_types.get(agent_id, "unknown-subagent")
        per_agent_tool_counts[agent_id] = per_agent_tool_counts.get(agent_id, 0) + 1
        summary = _format_tool(tool_name, tool_input)
        elapsed = (datetime.now() - start_time).total_seconds()
        label = agent_type or agent_id
        deny_reason = enforcement.check_tool_use(
            tool_name, tool_input, run_path, agent_type, registered_agents,
            cwd=input_data.get("cwd"))
        status = "DENIED" if deny_reason else ""
        print(f"[{label:>12} {elapsed:6.0f}s | #{tool_count}] {status} {summary}".replace("]  ", "] "),
              flush=True)
        record = {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "agent": agent_id,
            "agent_type": agent_type or ("lead" if agent_id == "lead" else None),
            "type": "tool_use",
            "tool": tool_name,
            "summary": summary,
        }
        if deny_reason:
            record["denied"] = deny_reason
        _safe_write(trace_file, record)  # a closed trace must not disable enforcement
        if deny_reason:
            return enforcement.deny_output(deny_reason)
        return {}

    async def post_tool_hook(input_data, tool_use_id, context):
        """Check written PNG files for oversized figures; snapshot report.md."""
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        agent_id = input_data.get("agent_id", "lead")
        if tool_name in ("Bash", "Write"):
            file_path = tool_input.get("file_path", "")
            if file_path.endswith(".png"):
                _check_figure_size(file_path, agent_id)
        if tool_name == "Bash":
            fig_dir = os.path.join(run_path, "figures")
            if os.path.isdir(fig_dir):
                for fname in os.listdir(fig_dir):
                    if fname.endswith(".png"):
                        _check_figure_size(os.path.join(fig_dir, fname), agent_id)
        snapshot_report(run_path, snapshot_state)
        return {}

    async def subagent_start_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        agent_types[agent_id] = agent_type
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"\n--- SUBAGENT START: {agent_type} (id={agent_id}) [{elapsed:.0f}s] ---", flush=True)
        _safe_write(trace_file, {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_start",
            "agent_id": agent_id,
            "agent_type": agent_type,
        })
        return {}

    async def subagent_stop_hook(input_data, tool_use_id, context):
        agent_id = input_data.get("agent_id", "?")
        agent_type = input_data.get("agent_type", "?")
        elapsed = (datetime.now() - start_time).total_seconds()
        turns_used = per_agent_tool_counts.get(agent_id, 0)
        cap = max_turns.get(agent_type)
        if cap is None:
            approx_stop_reason = "unknown"
        elif turns_used >= cap - 2:
            approx_stop_reason = "ran_to_cap"
        else:
            approx_stop_reason = "completed_under_cap"
        print(
            f"--- SUBAGENT STOP: {agent_type} (id={agent_id}) [{elapsed:.0f}s] "
            f"turns={turns_used} {approx_stop_reason} ---\n",
            flush=True,
        )
        _safe_write(trace_file, {
            "ts": datetime.now().isoformat(),
            "elapsed_s": elapsed,
            "type": "subagent_stop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "turns_used": turns_used,
            "approx_stop_reason": approx_stop_reason,
        })
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_hook])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_hook])],
        "SubagentStart": [HookMatcher(matcher=None, hooks=[subagent_start_hook])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_hook])],
    }
