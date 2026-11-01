"""Agent registry, lead prompt, and hook infrastructure for the modeling pipeline."""

import os
import re

from claude_agent_sdk import AgentDefinition

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
                    "benchmark-locked-acceptance",
                    # v2 workstream B: the planner drafts decision_spec.yaml
                    # (toolkit/spec), which the owner signs.
                    "decision-spec"],
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


# Phase 48 δ: the lead prompt and the hooks live in their own modules; every
# name is imported back so `from agents import ...` is unchanged. Imported last:
# both read names defined above (_registry, EFFORT_FLOORS_PATH).
from agents.lead_prompt import (  # noqa: E402,F401
    LEAD_SYSTEM_PROMPT,
    load_effort_floors,
    _floor_by_id,
    render_uq_draw_floors,
    render_agent_roster,
    CROSS_MODEL_STAGE6,
    METHODS_STAGE6,
    build_lead_prompt,
)
from agents.hooks import (  # noqa: E402,F401
    REBUILD_AFTER,
    MAX_FIGURE_PIXELS,
    _check_figure_size,
    _safe_write,
    snapshot_report,
    WRITE_ALLOWED_AFTER,
    _gate_rounds,
    budget_note,
    writer_hold_reason,
    PLAN_AGENTS,
    CRITIC_PROMPT_HEADS,
    _PLAN_ACTIONS,
    _budget_tight,
    _plan_required,
    _lead_spawns_since,
    _list_missing,
    plan_hold_reason,
    critique_hold_reason,
    _SPAWN_START_GRACE_S,
    _trace_since,
    work_running,
    review_wait_reason,
    signoff_hold_reason,
    scope_hold_reason,
    create_hooks,
)
