"""Agent registry, lead prompt, and hook infrastructure for the modeling pipeline."""

import json
import os
import re
import shutil
import time
from datetime import datetime

from claude_agent_sdk import AgentDefinition, HookMatcher

from agents import (
    planner, data, modeler, analyst,
    critique_methods, critique_domain, critique_premortem,
    critique_presentation, critique_sufficiency, red_team,
    writer,
)
from agents import enforcement
import sys  # noqa: E402
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)  # lib.md_helpers (shared frontmatter rule)
import open_problems  # noqa: E402  (Phase 32: open-problem list, round plan, fix checks)
import decision_brief  # noqa: E402  (Phase 33: the signed decision brief)
import done_when  # noqa: E402  (Phase 34: a test for every finding)
import derived_rebuild  # noqa: E402  (Phase 39 β: the harness regenerates stale figures)

REBUILD_AFTER = ("modeler", "analyst")   # stages whose outputs the figures show

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
    from lib.md_helpers import split_frontmatter   # same rule as the validator
    block, _ = split_frontmatter(text)
    if block is None:
        return ""
    try:
        import yaml
        fm = yaml.safe_load(block) or {}
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

def smoke_settings() -> dict | None:
    """Phase 25 pre-launch smoke: MA_SMOKE_MODEL runs every agent (and the
    lead) on a cheap model with a small turn cap, so the WHOLE pipeline,
    through the writer saving its draft and the final gate, can be
    exercised in minutes for a few dollars before any paid run."""
    model = os.environ.get("MA_SMOKE_MODEL")
    if not model:
        return None
    return {"model": model, "max_turns": int(os.environ.get("MA_SMOKE_MAX_TURNS", "12"))}


def build_agents(question: str = "", run_dir: str | None = None
                 ) -> dict[str, AgentDefinition]:
    """Return all subagent definitions for the lead session.

    Phase 21 α: `question` gates disease-/framework-specific skills
    (see select_skills); `run_dir` is substituted for the `{run_dir}`
    placeholder in every subagent prompt (previously only the lead's
    prompt was substituted, so subagents saw a literal `{run_dir}`).
    """
    agents = _registry()
    smoke = smoke_settings()
    for name, d in agents.items():
        if smoke:
            d.model = smoke["model"]
            d.maxTurns = min(d.maxTurns or smoke["max_turns"], smoke["max_turns"])
        if d.skills:
            d.skills = select_skills(d.skills, question)
        if run_dir:
            d.prompt = d.prompt.replace("{run_dir}", run_dir)
    return agents


WRITER_BRIEF_NOTE = """

## The decision brief (Phase 33)

Open the report's first section by restating the signed decision brief in
plain words (decision_brief.yaml and decision_brief.signoff.json): the
decision, what the report answers and at what resolution, what impact is
measured against (the primary comparator), what is out of scope, and the
owner's answers to its open questions. If brief_amendments.yaml
exists, list each amendment and its reason there too. Answer each A* item
of the brief, at its stated resolution, before anything else.
"""


def _registry() -> dict[str, AgentDefinition]:
    return {
        "planner": AgentDefinition(
            description=planner.DESCRIPTION,
            prompt=planner.SYSTEM_PROMPT,
            tools=planner.TOOLS,
            model=enforcement.MODEL_DEEP,
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
            model=enforcement.MODEL_FAST,
            maxTurns=60,
        ),
        "modeler": AgentDefinition(
            description=modeler.DESCRIPTION,
            prompt=modeler.SYSTEM_PROMPT + done_when.WORKER_NOTE,
            tools=modeler.TOOLS,
            model=enforcement.MODEL_DEEP,
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
                    # Phase 42 ε: costs, packages and the optimizer come
                    # from toolkit/alloc (checked by alloc_toolkit_use.py).
                    "allocation-toolkit",
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
            prompt=analyst.SYSTEM_PROMPT + done_when.WORKER_NOTE,
            tools=analyst.TOOLS,
            model=enforcement.MODEL_DEEP,
            maxTurns=40,
            skills=["investigation-threads", "malaria-modeling",
                    "basic_epi_modeling", "surveillance"],
        ),
        "critique-methods": AgentDefinition(
            description=critique_methods.DESCRIPTION,
            prompt=critique_methods.SYSTEM_PROMPT + open_problems.FIX_CHECK_NOTE + decision_brief.CRITIC_NOTE
                   + done_when.CRITIC_NOTE,
            tools=critique_methods.TOOLS,
            model=enforcement.MODEL_DEEP,
            maxTurns=35,  # increased: now does parameter provenance checks with WebSearch
            skills=["malaria-modeling", "model-validation",
                    "parameter-estimation"],
        ),
        "critique-domain": AgentDefinition(
            description=critique_domain.DESCRIPTION,
            prompt=critique_domain.SYSTEM_PROMPT + open_problems.FIX_CHECK_NOTE + decision_brief.CRITIC_NOTE
                   + done_when.CRITIC_NOTE,
            tools=critique_domain.TOOLS,
            model=enforcement.MODEL_DEEP,
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
            model=enforcement.MODEL_DEEP,  # adversarial roleplay strength on a clean slate
            maxTurns=30,
            skills=["pre-mortem-domain", "adversarial-redteam",
                    "model-fitness", "modeling-strategy",
                    "malaria-modeling", "vectors", "vaccination",
                    "surveillance", "effect-size-priors"],
        ),
        "critique-presentation": AgentDefinition(
            description=critique_presentation.DESCRIPTION,
            prompt=critique_presentation.SYSTEM_PROMPT + open_problems.FIX_CHECK_NOTE + decision_brief.CRITIC_NOTE
                   + done_when.CRITIC_NOTE,
            tools=critique_presentation.TOOLS,
            model=enforcement.MODEL_FAST,
            maxTurns=40,  # bumped 25→30 post-measles, 30→40 post-malaria (30/30 ran_to_cap in round 2)
        ),
        "critique-redteam": AgentDefinition(
            description=red_team.DESCRIPTION,
            prompt=red_team.SYSTEM_PROMPT + open_problems.FIX_CHECK_NOTE + decision_brief.CRITIC_NOTE
                   + done_when.CRITIC_NOTE,
            tools=red_team.TOOLS,
            model=enforcement.MODEL_DEEP,  # adversarial judgment + WebFetch-heavy research
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
            model=enforcement.MODEL_DEEP,  # claim-strength judgment benefits from opus
            maxTurns=30,
            skills=["evidence-sufficiency", "critique-blockers-schema",
                    "effect-size-priors"],
        ),
        "writer": AgentDefinition(
            description=writer.DESCRIPTION,
            prompt=writer.SYSTEM_PROMPT + WRITER_BRIEF_NOTE,
            tools=writer.TOOLS,
            model=enforcement.MODEL_DEEP,  # needs to synthesize the most complex output
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
- decision_brief.yaml (Phase 33): what the answer must deliver

If critical sections are missing, re-spawn planner with specific feedback.

#### STAGE 1.5: SIGN-OFF OF THE DECISION BRIEF (Phase 33)
`decision_brief.yaml` is the target the whole run is judged against: the
decision and who makes it, what the report must answer and at what
resolution, the objective, constraints, accounting conventions, what is out
of scope, what impact is measured against (comparators: one primary, the
others sensitivities), the acceptance criteria, and open questions for the
owner. Check that it is faithful to the question, that the primary
comparator describes what actually happens without the plan, that its
open questions are decisions that matter (facts are the planner's to find),
and that the method assessment says why the requested framework fits. Nothing but planning runs until it is signed;
the harness says whether the owner signs (end your turn with
AWAITING_SIGNOFF and you are resumed with the owner's answers) or you do.
After sign-off the brief and success_criteria.yaml are frozen and the
benchmark bands are locked when the gate first sees them; record framing
changes you discover in `brief_amendments.yaml` (`amendments:` list of
{id: AM-1, item: <A/C/K/OS/CF/Q id>, change, reason}); acceptance criteria
cannot be changed in-run. Pass the owner's answers to every specialist.

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
floor applies to SUCCESSFUL draws, so request ~10% more. Run it once per
final model version of a round, not after each edit: its fingerprint covers
only the modules `outcome_fn` loads.

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
Spawn the critique agents simultaneously in a SINGLE response:
- critique-domain
- critique-redteam  (adversarial — see the adversarial-redteam skill)
- critique-presentation only once a draft (manuscript.md) exists
  (Phase 39 α). Before it, presentation polish (captions, styling,
  figure-text wording; category PRESENTATION) waits for the draft: it
  neither blocks nor escalates, and the harness refuses the
  presentation critic's round review. The figures themselves are
  regenerated by the harness after each modeler and analyst stage, so
  the domain and red-team critics, and the modeler, should use them to
  challenge the model every round.
(critique-methods joins them only when this run's prompt says so; E5
found it caught no planted defect the others missed.)

Do NOT use background: true. Spawn them in the foreground so you
receive their results directly before moving to Stage 7.

Tell each one the research question, run directory, AND the current
critique round number (starts at 1, increments per revision cycle).
Each critique agent writes TWO files:
- `critique_{name}.md` — human-readable prose (as before)
- `critique_{name}.yaml` — machine-readable blocker manifest (see the
  `critique-blockers-schema` skill for the required schema)

The red-team agent (critique-redteam) is adversarial: its job is to find
what the other critics would miss — cross-file numeric discrepancies,
aggregate claims that exceed external totals, methodological fidelity
gaps vs cited prior work, data-vintage vs decision-year mismatches, and
hidden operational assumptions. It uses prefix `R-` for blocker IDs and
has the same architectural-veto authority (may set
`structural_mismatch.detected: true`) as domain (and methods, when on).

#### Spawn-prompt template (MANDATORY)

When invoking each critique agent via the Agent tool, the prompt you
pass MUST begin with the literal line `This is critique round N.` where
N is the current round number (starting at 1). Without this line the
critique agent falls back to round 1 and emits a YAML with `round: 1`
regardless of the actual round, which the validator will reject with a
schema error and cost you a revision cycle. The harness refuses methods,
domain, presentation and red-team spawns that start any other way, except
`This is a fix check after round N.` (Step 2b) and `This is a repair of
<file>.` (fix only that file's format, for example a schema error).

Example spawn-prompt opening for round 2+:

```
This is critique round 2. Research question: [verbatim question].
Run directory: runs/<run_name>. Read the prior round's
critique_<name>.yaml and populate carried_forward for every HIGH or
MEDIUM blocker from round 1 (still_present true or false with
resolved_evidence). See the critique-blockers-schema skill.
```

Wait for all of them to complete.

### STAGE 7: MECHANICAL DECISION GATE

If the validator reports `artifact_integrity_violation` for a file, that
gate output was written outside its producer script. The fix is to re-run
its producer (`python3 scripts/<producer>.py {run_dir}`, or ask the owning
specialist to), which rewrites the file and resolves the violation. Never
edit the file or `artifact_integrity.json`.


The STAGE 7 decision is NOT a free judgment call. You compute it
mechanically from the critique YAML files. You do not get to
override the rules with prose reasoning.

#### Step 1: Run the validator

After the critique YAMLs exist (domain, presentation, redteam, plus
methods when enabled), invoke the validator via Bash with ALL THREE gate flags:

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
- Schema-check every critique YAML file. Exit code 3 = schema error;
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

#### Step 2b: Plan every open problem (Phase 32)

After a PATCH_OR_RETHINK or RETHINK_STRUCTURAL gate, the gate has written
`open_problems.md`: every open HIGH and MEDIUM finding, soonest-blocking
first, with how many rounds each MEDIUM has before it becomes blocking.
Before the next round's work, write `{run_dir}/round_plan.yaml`:

```yaml
round: <N>            # the gate round just run
items:
  R-005: {action: fix, stage: ANALYZE}
  R-008: {action: rethink, stage: MODEL}
  R-017: {action: declare, reason: "<why it cannot be fixed with the data or tools available>"}
  P-007: {action: after_write}
  D-023: {action: defer, reason: "<why not this round>"}
  "REG-*": {action: fix, stage: MODEL}
```

Every id needs an action: `fix` or `rethink` (with the stage that does it),
`declare` (cannot be fixed in this run; goes in scope_declaration.yaml),
`after_write` (write-up findings only), or `defer` (with a reason; not for
blocking findings or ones that block at the next gate). Patterns such as
`REG-*` are allowed for gate findings only; critic findings are listed by id.
Moderate findings count: a MEDIUM left alone for four rounds becomes
blocking, and several reaching that point together sank an earlier run.
Decide early which findings cannot be fixed with what is available and
declare them in round 1 or 2, while the scope-declaration allowance is free,
so the rounds go to what can be fixed.

The harness enforces this: planner, data-agent, modeler and analyst spawns
are refused until the plan covers the latest list, and the round's critique
is refused until every `fix` / `rethink` id has been named in a spawn prompt
to its stage's agent (PLAN planner, DATA data-agent, MODEL modeler, ANALYZE
analyst, WRITE writer). Once that work is handed out, the actions in the
plan are fixed for the round (you may add items a gate rerun lists, not
change these): a fix that did not work stays open, and the next gate lists
it again for the next round's plan. Each critic finding carries a `done_when`
test (shown in open_problems.md): the gate closes a finding when its test
passes and reopens it if a later change breaks it, so tell each agent to run
`python3 scripts/done_when.py check {run_dir} <IDs>` before it returns.
Agents run in the background, so critics
and the writer are refused while a planner, data-agent, modeler or analyst
is still working; wait for each result before spawning them.

#### Step 3: Execute the action

The validator has already decided for you. Your job is to carry it out:

- **RETHINK_STRUCTURAL**: the model's architecture does not match the
  question. Do NOT patch. Do NOT DECLARE_SCOPE. Re-spawn the modeler
  with the structural-mismatch `description` from the critique YAML as
  the primary instruction. Reference modeling strategy Level
  progression where appropriate.

- **RUN_FAILED**: no rounds remaining and blocking findings stand. Write
  `RUN_FAILED` at the top of progress.md with the blocking findings.
  Then spawn the writer for a **not-accepted report** (Phase 29: every
  run ends with a report, so its failure can be studied). Tell the writer:
  the report's first section is "Status: not accepted by the final gate",
  listing each unresolved blocking finding from the latest critique and
  validator output in plain words; the model is presented as what was
  built and why it fell short, never as a recommendation; the allocation,
  if any, is labelled illustrative. Run the render step, then end.

- **PATCH_OR_RETHINK**: HIGH blockers remain and rounds are left. You
  choose PATCH vs RETHINK using the heuristics below. ACCEPT is
  forbidden. DECLARE_SCOPE is forbidden.

  * **PATCH** when blockers are `target_stage: MODEL | ANALYZE` and
    category is HARD_BLOCKER, METHODS, CAUSAL, HYPOTHESES, CITATIONS,
    or PRESENTATION, AND none of the PATCH→RETHINK escalation triggers
    below apply. Re-spawn the target_stage agent with the verbatim
    `claim` + `fix_requires` text from each blocker, and every MEDIUM your
    round plan marks `fix` for that stage, each named by id.

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
      model doesn't support X" or, once a draft exists, re-spawn WRITE
      with "the report claims Y, but the underlying model produces Z". Document the
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

  * **Stalled findings (Phase 47 ε)** — when the validator output lists
    `stalled_findings:`, those gate findings have not moved for the
    number of rounds set in effort_floors.yaml. Plan a diagnosis, not
    another patch: spawn the modeler to record one per finding in
    `models/stall_diagnoses.yaml` (rigor manifest entry
    `stall_diagnoses`), or declare the finding in scope_declaration.yaml.

- **DECLARE_SCOPE**: HIGH blockers remain and rounds exhausted, or
  (Phase 39) every outstanding item is a gate finding that
  `scope_declaration.yaml` declared at this gate and the previous one,
  with no critic blocker open: the remaining rounds would not change
  them, so the run goes to the write-up now. Declare a gate finding as
  soon as you know iteration cannot close it (a locked target you
  believe is wrong, a real instability in the answer): it then stops
  holding the run open one round later. Critic findings are never
  declared away early. Before spawning the writer, you MUST write
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
- DECLARE_SCOPE while rounds remain, unless the validator itself
  returned it (rule 3a).
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

#### Fix check before the next full critique (Phase 32)

When the fixes for round N are done, and before the next STAGE 6, spawn
each critic that raised a planned `fix` / `rethink` item in fix-check mode,
in parallel, with a prompt beginning `This is a fix check after round N.`
and listing only its ids. Each re-checks those items and what the fix
changed, and writes `fix_check_rN_<critic>.yaml` (verdict fixed / partly /
not_fixed per id, plus any new problem the fix caused). This is a short
session; it exists so a fix that misses, or breaks something else, is caught
while the round can still correct it. Send each item not fixed, and each new
HIGH problem, back to its stage's agent once, naming the id and quoting the
critic's evidence; then run the full STAGE 6 critique. The harness refuses
the full critique until the fix checks exist and that one return trip has
been made; it skips both when the budget cannot cover another round.

The first write-up waits for the gate: the harness refuses a writer spawn
while the latest action is PATCH_OR_RETHINK and the budget covers another
review round, until a draft exists. Blockers with `target_stage: WRITE`
neither block nor escalate before a write-up exists; carry them to STAGE 8.
After each subagent returns, the harness tells you the budget spent and the
review rounds it still covers; plan the remaining work to fit.

### STAGE 8: WRITE
Spawn the "writer" agent. Tell it the run directory.
Wait for completion. Verify {run_dir}/manuscript.md exists (the writer's
draft; the CLI does not let subagents write report.md). The render step
below builds report.md from it.

Presentation pass (Phase 39 α): once the draft exists, spawn
critique-presentation (`This is critique round N.`) on it, with the
after-write findings from open_problems.json, and route what it raises
to the writer (wording, captions) or the analyst (figures) before the
final gate.

#### STAGE 8.1: RENDER CLAIM REFERENCES (Phase 18 α — REQUIRED)

After manuscript.md is written and BEFORE writer-QA / coherence audit, run:

```bash
python3 scripts/render_claims.py {run_dir}
```

This rebuilds `{run_dir}/models/claims_ledger.yaml` from the analyst's
`models/claims_spec.yaml` (values read from the source artifacts) and
substitutes every `[CLAIM:claim_id]` reference
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
  to manuscript.md. Keep every number as a [CLAIM:id] reference." The
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
ACCEPT (or DECLARE_SCOPE from the validator), treat it exactly like a
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
  (DECLARE_SCOPE comes only from the validator: at rounds exhausted, or
  when only long-declared gate findings remain).
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


CROSS_MODEL_STAGE6 = """
CROSS-MODEL CRITIC (enabled for this run). In STAGE 6 of every round, in
parallel with the critique agents, run via Bash (in the background; it
takes ~5-10 min, wait for it before STAGE 7):
    python3 scripts/cross_model_critic.py {run_dir} --round <N>
It is an independent reviewer from a different model family (gpt-6.1-sol)
and writes critique_crossmodel.yaml itself. Do not redirect its output into
the run dir and never edit critique_crossmodel.*; to refresh it, re-run the
script. Route X- blockers to the owning agent exactly like M-/D-/P-/R-.
"""


METHODS_STAGE6 = """
METHODS CRITIC (enabled for this run). In STAGE 6 of every round, also
spawn critique-methods in the same response as the other critique agents
(same spawn-prompt template). It writes critique_methods.yaml (prefix M-);
the validator requires that file for this run.
"""


def build_lead_prompt(question: str, run_dir: str, max_rounds: int,
                      resume_context: str = "", cross_model: bool = False,
                      methods_critic: bool = False) -> str:
    """Build the user-facing prompt for the lead agent."""
    prompt = (
        f"Research question: {question}\n\n"
        f"Run directory: {run_dir}\n"
        f"Max critique-revision rounds: {max_rounds}\n"
    )
    if methods_critic:
        # E5: opt-in; 0/54 planted defects caught only by methods (~$45/round).
        prompt += METHODS_STAGE6
    if cross_model:
        # Phase 21 ζ: gpt-6.1-sol found eligible-population errors that
        # five same-family reviewers missed.
        prompt += CROSS_MODEL_STAGE6.replace("{run_dir}", run_dir)
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


WRITE_ALLOWED_AFTER = ("ACCEPT", "DECLARE_SCOPE", "RUN_FAILED")


def _gate_rounds(run_path: str) -> list[dict]:
    rows = []
    try:
        for line in open(os.path.join(run_path, "validator_history.jsonl"), errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        pass
    return rows


def budget_note(status: dict | None, n_rounds: int) -> str | None:
    """Phase 31: the lead's view of the dollar cap (E7 attempt 6 spent its
    last round on a writing loop without knowing the budget was nearly gone)."""
    if not status or not status.get("cap"):
        return None
    spent, cap, reserve = status["spent"], status["cap"], status["reserve"]
    left = cap - reserve - spent
    line = (f"[harness] Budget: about ${spent:.0f} of ${cap:.0f} spent (estimate); ${reserve:.0f} is held for "
            f"the final write-up, leaving about ${max(left, 0):.0f} for modeling")
    if n_rounds:
        per = spent / n_rounds
        line += (f", about {max(left, 0) / per:.1f} more review round(s) at the ${per:.0f} per round "
                 f"average so far ({n_rounds} gate round(s))")
    return line + ". Plan the remaining work to fit; if the budget runs out the harness writes the report."


def writer_hold_reason(run_path: str, status: dict | None, max_rounds: int | None) -> str | None:
    """Phase 31: the first write-up waits for the gate. Returns why a writer
    spawn is refused now, or None. Allowed after ACCEPT / DECLARE_SCOPE /
    RUN_FAILED, in the final round, when less than one more round of budget
    is left, or once a draft exists (revisions)."""
    if any(os.path.exists(os.path.join(run_path, f)) for f in ("manuscript.md", "report.md")):
        return None
    rows = _gate_rounds(run_path)
    last = rows[-1] if rows else {}
    if str(last.get("action") or "") in WRITE_ALLOWED_AFTER:
        return None
    rnd = max((int(r.get("round") or 0) for r in rows), default=0)
    if max_rounds and rnd >= max_rounds:
        return None
    if status and status.get("cap") and rows:
        left = status["cap"] - status["reserve"] - status["spent"]
        if left < status["spent"] / max(1, len({r.get("round") for r in rows})):
            return None
    return ("The write-up waits for the gate: the latest gate action is "
            f"{last.get('action') or 'none yet'}, and the budget covers another review round. "
            "Fix the blocking findings, re-run the critiques and the gate; spawn the writer after ACCEPT, "
            "DECLARE_SCOPE or RUN_FAILED. Findings aimed at the write-up do not block or escalate before it exists.")


PLAN_AGENTS = ("planner", "data-agent", "modeler", "analyst")
CRITIC_PROMPT_HEADS = ("This is critique round", "This is a fix check", "This is a repair of")
_PLAN_ACTIONS = ("PATCH_OR_RETHINK", "RETHINK_STRUCTURAL")


def _budget_tight(status: dict | None, rows: list[dict]) -> bool:
    """Less than one more review round of budget left (at the average so far)."""
    if not status or not status.get("cap") or not rows:
        return False
    left = status["cap"] - status["reserve"] - status["spent"]
    return left < status["spent"] / max(1, len({r.get("round") for r in rows}))


def _plan_required(run_path: str) -> dict | None:
    """Phase 32: the open-problem list the next round's work must plan for,
    or None (no gate yet, gate not asking for work, or a stale list)."""
    rows = [r for r in _gate_rounds(run_path) if not r.get("post_write")]
    if not rows or str(rows[-1].get("action") or "") not in _PLAN_ACTIONS:
        return None
    doc = open_problems.load_open(run_path)
    if doc is None or int(doc.get("round") or -1) != int(rows[-1].get("round") or -2):
        return None
    return doc


def _lead_spawns_since(run_path: str, since: float, with_time: bool = False) -> list[tuple]:
    """(subagent_type, ids named in its prompt) for lead spawns after `since`
    (an mtime), read back from trace.jsonl so a resumed session sees them."""
    out = []
    try:
        for line in open(os.path.join(run_path, "trace.jsonl"), errors="replace"):
            if '"mentions"' not in line:
                continue
            try:
                r = json.loads(line)
                t = datetime.fromisoformat(r["ts"]).timestamp()
                if t >= since and not r.get("denied"):
                    out.append((r.get("subagent_type"), set(r.get("mentions") or ()))
                               + ((t,) if with_time else ()))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return out


def _list_missing(run_path: str) -> str | None:
    """Codex review: the gate asked for a plan but its open-problem list is
    missing or older than that gate run: refuse rather than wave work through."""
    rows = [r for r in _gate_rounds(run_path) if not r.get("post_write")]
    if not rows or str(rows[-1].get("action") or "") not in _PLAN_ACTIONS:
        return None
    if _plan_required(run_path) is not None:
        return None
    n = rows[-1].get("round")
    return (f"The round {n} gate asked for more work, but {open_problems.OPEN_JSON} for round {n} is missing or "
            f"stale. Re-run the gate for round {n} (scripts/validate_critique_yaml.py with --current-round {n}) "
            "so it writes the open-problem list, then plan it.")


def plan_hold_reason(run_path: str) -> str | None:
    """Phase 32 α: work for the next round waits for round_plan.yaml."""
    doc = _plan_required(run_path)
    if doc is None:
        return _list_missing(run_path)
    errors = open_problems.check_plan(run_path)
    if not errors:
        return None
    return (f"Before the next round's work, write {open_problems.PLAN_FILE} (round: {doc['round']}) giving every "
            f"open problem in open_problems.md an action. Not yet: " + "; ".join(errors)[:1500])


def critique_hold_reason(run_path: str, status: dict | None) -> str | None:
    """Phase 32 α/β: the round's full critique waits until every planned
    fix went to its stage's agent, the critics checked their fixes, and what
    was not fixed went back once. Skipped when the budget cannot cover it."""
    doc = _plan_required(run_path)
    if _budget_tight(status, _gate_rounds(run_path)):
        return None
    if doc is None:
        return _list_missing(run_path)
    errors = open_problems.check_plan(run_path)
    if errors:
        return plan_hold_reason(run_path)
    plan_path = os.path.join(run_path, open_problems.PLAN_FILE)
    missing = open_problems.unsent(open_problems.planned_work(run_path),
                                   _lead_spawns_since(run_path, open_problems.round_start(run_path)))
    if missing:
        return ("The round's critique waits until every planned fix has been handed to the agent that does it. "
                "Not yet named in a spawn prompt: " + ", ".join(missing)[:1200])
    absent, redo = open_problems.check_fix_checks(run_path)
    if absent:
        return ("Before the round's full critique, ask each critic to check its own planned fixes: spawn it with "
                f"a prompt beginning `This is a fix check after round {doc['round']}.` listing its ids. Missing: "
                + "; ".join(absent)[:1200])
    if redo:
        since = open_problems.redo_since(run_path)
        start = min(since.values(), default=open_problems.fix_check_mtime(run_path) or 0)
        later = [(a, got, t) for a, got, t in _lead_spawns_since(run_path, start, with_time=True)]
        left = [f"{i} -> {agent}" for agent, ids in redo.items() for i in ids
                if not any(a == agent and i in got and t >= since.get(i, start) for a, got, t in later)]
        if left:
            return ("The fix checks found items not fixed or new problems the fixes caused. Send each back to its "
                    "agent once (name the id and quote the critic's evidence), then run the full critique: "
                    + ", ".join(left)[:1200])
    return None


_SPAWN_START_GRACE_S = 120


def _trace_since(run_path: str, since: float):
    try:
        for line in open(os.path.join(run_path, "trace.jsonl"), errors="replace"):
            try:
                r = json.loads(line)
                if datetime.fromisoformat(r["ts"]).timestamp() >= since:
                    yield r
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        return


def work_running(run_path: str, since: float, kinds=PLAN_AGENTS, now: float | None = None) -> list[str]:
    """Phase 32 γ: work agents launched since `since` (this session) that have
    not finished. The CLI launches agents in the background ("Async agent
    launched"), so the lead can ask a critic to review work still being done.
    An agent cut off at its turn limit logs a task_notification and no
    subagent_stop (E7 attempt 6: 5 of 57), so either counts as finished. A
    spawn not yet started counts for a short grace period, since the critic
    may be spawned in the same message."""
    now = time.time() if now is None else now
    started, done, spawned, starts = {}, set(), {}, {}
    for r in _trace_since(run_path, since):
        t = r.get("type")
        if t == "subagent_start" and r.get("agent_type") in kinds:
            started[r.get("agent_id")] = (r["agent_type"], r["ts"][11:19])
            starts[r["agent_type"]] = starts.get(r["agent_type"], 0) + 1
        elif t == "subagent_stop":
            done.add(r.get("agent_id"))
        elif t in ("task_notification", "task_updated"):   # task_updated: terminal statuses only (main.py)
            done.add(r.get("task_id"))
        elif r.get("subagent_type") in kinds and r.get("agent") == "lead" and not r.get("denied"):
            spawned.setdefault(r["subagent_type"], []).append(datetime.fromisoformat(r["ts"]).timestamp())
    out = [f"{k} (since {ts})" for a, (k, ts) in started.items() if a not in done]
    for k, times in spawned.items():
        out += [f"{k} (launching)" for t in sorted(times)[starts.get(k, 0):] if t >= now - _SPAWN_START_GRACE_S]
    return out


def review_wait_reason(run_path: str, sub: str | None, since: float) -> str | None:
    """Phase 32 γ: critics and the writer review finished work only. The
    pre-mortem reviews the plan, so it waits for the planner alone."""
    sub = str(sub or "")
    if not (sub.startswith("critique-") or sub == "writer"):
        return None
    busy = work_running(run_path, since, ("planner",) if sub == "critique-premortem" else PLAN_AGENTS)
    if not busy:
        return None
    return (f"{sub} reviews finished work, and these agents are still working: {', '.join(busy)[:600]}. Wait "
            f"for their results (each comes back to you when it finishes), then spawn {sub}.")


def signoff_hold_reason(run_path: str, mode: str | None, run_dir_rel: str = "{run_dir}") -> str | None:
    """Phase 33 α: nothing but planning happens before the decision brief is
    signed (owner mode: the owner signs and the harness resumes the lead;
    lead mode, for unattended test runs: the lead signs with its reason)."""
    if mode not in ("owner", "lead"):
        return None
    st = decision_brief.status(run_path)
    if st == "signed":
        return None
    if st == "changed":
        return (f"{decision_brief.BRIEF} or {decision_brief.CRITERIA} changed after sign-off. They are frozen: run "
                f"`python3 scripts/decision_brief.py restore {run_dir_rel}` to put the signed versions back, and "
                f"record framing changes in {decision_brief.AMENDMENTS}.")
    if st in ("absent", "invalid"):
        errs = "; ".join(decision_brief.load_brief(run_path)[1])[:900]
        return (f"Before any other work the planner writes {decision_brief.BRIEF} (what the answer must deliver; see "
                f"STAGE 1). Re-spawn the planner with these problems: {errs}")
    if mode == "owner":
        return ("The decision brief is ready and waits for the owner's sign-off. Do not start other work. End your "
                "turn now with the line AWAITING_SIGNOFF; the harness resumes you with the owner's answers.")
    return ("Review the decision brief against the question, then sign it: python3 scripts/decision_brief.py sign "
            f"{run_dir_rel} --by lead --accept-proposed --note \"<why it is an adequate target>\" (or answer the "
            "open questions with --answers <file>).")


def create_hooks(run_path: str, trace_file, start_time: datetime,
                 registered_agents=None, max_turns: dict[str, int] | None = None,
                 integrity=None, budget_status=None, max_rounds: int | None = None,
                 hold_writer: bool = True, signoff: str | None = None):
    """Create hook config for the lead query() session.

    Phase 21 α: PreToolUse also enforces file ownership and the agent
    registry via agents.enforcement.check_tool_use (deny, not warn);
    PostToolUse snapshots report.md whenever it changes.
    `registered_agents` defaults to the full registry.
    """

    tool_count = 0
    per_agent_tool_counts: dict[str, int] = {}
    turn_warned: dict[str, set] = {}
    agent_types: dict[str, str] = {}
    snapshot_state: dict = {}
    session_t0 = time.time()   # Phase 32 γ: background agents end with their session
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
        spawn = agent_id == "lead" and tool_name in ("Agent", "Task")
        sub = tool_input.get("subagent_type") if spawn else None
        if not deny_reason and spawn and sub != "planner":
            deny_reason = signoff_hold_reason(run_path, signoff, run_path)
        if (not deny_reason and signoff == "owner" and tool_name == "Bash"
                and re.search(r"decision_brief\.py\b.*\bsign\b", str(tool_input.get("command") or ""), re.S)):
            deny_reason = "The owner signs the decision brief in this run; agents cannot sign it."
        if not deny_reason and spawn:
            deny_reason = review_wait_reason(run_path, sub, session_t0)
        if not deny_reason and hold_writer and sub == "writer":
            deny_reason = writer_hold_reason(run_path, _status(), max_rounds)
        if not deny_reason and hold_writer and sub in PLAN_AGENTS:
            deny_reason = plan_hold_reason(run_path)
        head = str(tool_input.get("prompt") or "").lstrip()
        if not deny_reason and hold_writer and sub in open_problems.FIX_CHECK_CRITICS \
                and not head.startswith(CRITIC_PROMPT_HEADS):
            # Codex review: the round's holds keyed on one phrase, so a reworded
            # full critique skipped them. Every critic spawn now says what it is.
            deny_reason = (f"Start a {sub} prompt with one of: `This is critique round N.` (the round's "
                           "review), `This is a fix check after round N.` (check planned fixes), or "
                           "`This is a repair of <file>.` (fix only that file's format, no new review).")
        if not deny_reason and hold_writer and sub == "critique-presentation" \
                and head.startswith("This is critique round") and not open_problems._written(run_path):
            # Phase 39 α: presentation is reviewed once, on the first draft; before it,
            # its findings could not be acted on (they wait for the draft).
            deny_reason = ("critique-presentation reviews the write-up, so its round review waits for the first "
                           "draft (manuscript.md). Spawn critique-domain and critique-redteam for this round; run "
                           "critique-presentation after STAGE 8 on the draft.")
        if not deny_reason and hold_writer and sub in open_problems.FIX_CHECK_CRITICS \
                and head.startswith("This is critique round"):
            deny_reason = critique_hold_reason(run_path, _status())
        if not deny_reason and hold_writer and sub in PLAN_AGENTS and _plan_required(run_path) is not None:
            try:   # Phase 32 γ: the round's actions are fixed once work is handed out
                if open_problems.freeze(run_path) and integrity is not None:
                    integrity.harness_wrote(open_problems.FROZEN_FILE)
            except OSError:
                pass
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
        if spawn:
            record["subagent_type"] = sub
            if str(sub or "").startswith("critique-"):
                head = str(tool_input.get("prompt") or "").lstrip()
                record["critique_kind"] = ("full" if head.startswith("This is critique round")
                                           else "fix_check" if head.startswith("This is a fix check")
                                           else "repair" if head.startswith("This is a repair of") else "other")
            record["mentions"] = open_problems.mentions(str(tool_input.get("prompt") or ""), run_path)
        if deny_reason:
            record["denied"] = deny_reason
        if tool_name == "Bash":
            # Phase 42 ε: which harness scripts the call runs, with its full command
            # (the summary is cut at 80 characters), so scripts/convergence_report.py
            # can count uncertainty runs and gate runs per round.
            try:
                from agents.integrity import harness_scripts_run
                cmd = str(tool_input.get("command") or "")
                ran = harness_scripts_run(cmd, input_data.get("cwd"))[0]
                if ran:
                    record["scripts_run"] = ran
                    record["command"] = cmd[:1000]
            except Exception:  # noqa: BLE001 — bookkeeping must not break enforcement
                pass
        _safe_write(trace_file, record)  # a closed trace must not disable enforcement
        if deny_reason:
            return enforcement.deny_output(deny_reason)
        if integrity is not None:
            # Phase 23 α: record what this allowed call may change.
            integrity.on_pre(tool_use_id, tool_name, tool_input, agent_type,
                             cwd=input_data.get("cwd"))
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
        if integrity is not None:
            for ev in integrity.on_post(tool_use_id):
                print(f"[INTEGRITY] unexplained {ev['change']} of {ev['file']} "
                      f"(in flight: {ev['in_flight'] or 'nothing'})", flush=True)
                _safe_write(trace_file, {"ts": ev["ts"], "type": "integrity_violation", **ev})
        if agent_id == "lead" and tool_name in ("Agent", "Task"):
            note = budget_note(_status(), len({r.get("round") for r in _gate_rounds(run_path)}))
            if note:
                return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
        warning = turn_warning(agent_id)
        if warning:
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": warning}}
        return {}

    def _status():
        try:
            return budget_status() if budget_status else None
        except Exception:  # noqa: BLE001 — a status error must not break a tool call
            return None

    def turn_warning(agent_id: str) -> str | None:
        """Phase 31: tell a subagent before its turn limit cuts it off, so it
        saves its outputs (E7 attempt 6: 6 of 47 spawns hit the limit; the red
        team's first session ended with no output). Tool calls are counted, a
        slight over-count of turns, so the warning comes early, never late."""
        if agent_id == "lead":
            return None
        cap = max_turns.get(agent_types.get(agent_id, ""))
        used = per_agent_tool_counts.get(agent_id, 0)
        if not cap or cap < 10:
            return None
        left = cap - used
        done = turn_warned.setdefault(agent_id, set())
        level = ("early" if used >= min(int(cap * 0.85), cap - 4) and "early" not in done
                 else "final" if left <= 3 and "final" not in done else None)
        if level is None:
            return None
        turn_warned[agent_id].add(level)
        _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "turn_warning", "agent_id": agent_id,
                                 "agent_type": agent_types.get(agent_id), "used": used, "cap": cap})
        if level == "final":
            return (f"[harness] About {max(left, 0)} turns left of your {cap}. Your session will be cut off at the "
                    "limit. Write your required output files NOW, then stop; summarise what is unfinished in them.")
        return (f"[harness] You have used about {used} of your {cap} turns. Start writing your required output "
                "files now; a session cut off at the limit loses whatever is not saved to disk.")

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
        # Phase 39 β: figures drawn from this stage's outputs are regenerated by
        # the harness before anyone reads them (the analyst, the critics, the gate).
        if agent_type in REBUILD_AFTER:
            used = 0.0
            if integrity is not None and os.path.exists(os.path.join(run_path, derived_rebuild.pipeline.SPEC)):
                # S1: the run's declared pipeline is rebuilt by the harness first (its
                # outputs are harness-owned); figure sidecars cover what it does not declare.
                try:
                    import asyncio
                    t_p = time.time()
                    prep = await asyncio.to_thread(derived_rebuild.pipeline.harness_build, run_path, integrity,
                                                   derived_rebuild.HOOK_BUDGET_S * 0.7)
                    used = time.time() - t_p
                    pline = derived_rebuild.pipeline.summary_line(prep)
                    print(f"[PIPELINE] {pline}", flush=True)
                    _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "pipeline_build",
                                             "after": agent_type, "summary": pline, "ok": prep.get("ok"),
                                             "steps": [{"id": r["id"], "status": r.get("status")}
                                                       for r in prep.get("steps", [])],
                                             "errors": prep.get("errors")})
                except Exception as e:  # noqa: BLE001 — a build failure must not end the session
                    print(f"[PIPELINE] failed: {type(e).__name__}: {e}", flush=True)
            try:
                import asyncio
                rep = await asyncio.to_thread(derived_rebuild.rebuild, run_path,
                                              budget_s=max(0.0, derived_rebuild.HOOK_BUDGET_S - used),
                                              integrity=integrity)
                line = derived_rebuild.summary_line(rep)
                print(f"[REBUILD] {line}", flush=True)
                _safe_write(trace_file, {"ts": datetime.now().isoformat(), "type": "derived_rebuild",
                                         "after": agent_type, "summary": line,
                                         "rebuilt": len(rep["rebuilt"]), "still_stale": rep["still_stale"],
                                         "provenance_rerecorded": rep.get("provenance_rerecorded", []),
                                         "failed": [r["script"] for r in rep["runs"] if r["returncode"] != 0]})
            except Exception as e:  # noqa: BLE001 — a rebuild failure must not end the session
                print(f"[REBUILD] failed: {type(e).__name__}: {e}", flush=True)
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_hook])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_hook])],
        "SubagentStart": [HookMatcher(matcher=None, hooks=[subagent_start_hook])],
        "SubagentStop": [HookMatcher(matcher=None, hooks=[subagent_stop_hook])],
    }
