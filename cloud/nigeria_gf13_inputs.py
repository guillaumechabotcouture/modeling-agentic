#!/usr/bin/env python3
"""Inputs for the v2 run of the E7 Nigeria question scoped to the 13 Global Fund-supported states.

    python3 cloud/nigeria_gf13_inputs.py build <attempt-8 run dir> <out dir>     # source-level files, README, manifest
    python3 cloud/nigeria_gf13_inputs.py verify <dir>                           # every file matches manifest.json
    python3 cloud/nigeria_gf13_inputs.py upload <dir> [--account A]             # to inputs/nigeria_gf13_v2/ in storage
    python3 cloud/nigeria_gf13_inputs.py fetch <dest> [--account A]             # back from storage, sha256-checked
    python3 cloud/nigeria_gf13_inputs.py stage-tree <dir> <launch tree>         # local-only commit for the launcher
    python3 cloud/nigeria_gf13_inputs.py question <dir>                         # the v2 question text (stdout)
    python3 cloud/nigeria_gf13_inputs.py --self-test

This file holds no data. The data are derived from DHS, MAP, WorldPop, NASA POWER, the
hbhi-nigeria-publication-2021 repository, Global Fund Data Explorer and published papers
(README.txt in the built folder lists each file's source and licence). DHS terms do not
clearly allow redistribution, so the files never enter this public repository: they live
in ~/modeling-agentic-keep/nigeria_gf13_v2/ and in the project storage account.

How a launch passes them: the v2 harness (agents/stage_machine.stage_inputs) copies every
repository file the question names into the run's data/inputs/ with its sha256. The cloud
launcher ships `git archive HEAD` of its launch tree, so `stage-tree` copies the files into
<tree>/data/nigeria_gf13/ and commits them on a local branch whose push URL is disabled
(the commit can never reach GitHub). The question names those paths (`question`).
/data/nigeria_gf13/ is in .gitignore so they cannot be committed here by accident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

TREE_DIR = "data/nigeria_gf13"           # inside a launch tree, the paths the question names
STORAGE_PREFIX = "inputs/nigeria_gf13_v2"
LOCAL_BRANCH = "local-nigeria-gf13-inputs"
MANIFEST = "manifest.json"
GF = "Global Fund"

QUESTION_E7 = (
    "Build an agent-based model of malaria transmission across Nigeria's 774 Local Government Areas (LGAs) using "
    "the Starsim framework. The model should include ITNs/LLINs (with PBO and dual-AI net types), indoor residual "
    "spraying (IRS), seasonal malaria chemoprevention (SMC), and case management (ACTs). Calibrate to LGA-level "
    "PfPR estimates from the Malaria Atlas Project and state-level DHS/NMIS data. The goal is to produce an "
    "optimized, cost-constrained resource allocation across LGAs for Nigeria's Global Fund GC7 request (~$320M "
    "malaria component), identifying which LGAs should receive which intervention packages.")
SCOPE = ("Scope (owner decision, 2026-11-01): restrict the analysis and the allocation to the 13 states supported "
         "by the Global Fund (309 LGAs); the GC7 envelope applies to these states.")

# --- sources (README text) -------------------------------------------------------------
A8 = "E7 attempt 8 run (2026-10-28), data/processed/"
SOURCES = {
    "lga_geography_population.csv": dict(
        rows="309 LGAs", from_="lga_master_774.csv (columns kept as downloaded; no rescaling)",
        source="WorldPop Global2 R2025A 2024 constrained 1 km age-sex rasters (data.worldpop.org, "
               "AgeSex_structures/Global_2015_2030/R2025A/2024/NGA); LGA polygons and names from the hbhi-nigeria "
               "shapefile (NGA_LGAs 191016) and geoBoundaries NGA ADM2 (GRID3 2022)",
        citation="WorldPop (www.worldpop.org) Global2 R2025A, doi:10.5258/SOTON/WP00845; Runfola et al. 2020 "
                 "geoBoundaries, PLoS ONE 15(4):e0231866",
        licence="WorldPop CC BY 4.0; geoBoundaries gbOpen CC BY 4.0",
        processing="zonal sums of the rasters by LGA polygon (attempt 8 scripts_data/build_lga_geo.py); u5 = age "
                   "bands 0 + 1; 5-14 = bands 5 + 10; 15plus = remaining bands; lon/lat = polygon representative "
                   "point; area from the polygon"),
    "lga_map_estimates.csv": dict(
        rows="309 LGAs",
        from_="lga_master_774.csv map_* columns",
        source="Malaria Atlas Project GeoServer WCS (data.malariaatlas.org/geoserver/ows), 202608 release: "
               "Pf parasite rate 2-10 (2000-2025), Pf incidence, ITN use and access, antimalarial effective "
               "treatment, IRS coverage (2010/15/18/21/24) and 202508 Pf mortality",
        citation="Malaria Atlas Project, 2026 release (malariaatlas.org); Weiss et al. 2019 Lancet 394:322; "
                 "Bertozzi-Villa et al. 2021 Nat Commun 12:3589",
        licence="MAP maps and rasters: CC BY 3.0 Unported (MAP open-access policy)",
        processing="population-weighted (WorldPop 2024) zonal mean of each raster per LGA; values are the rasters' "
                   "units (proportions; incidence per person-year; mortality per person-year)"),
    "lga_hbhi_attributes.csv": dict(
        rows="309 LGAs", from_="lga_master_774.csv hbhi columns",
        source="github.com/numalariamodeling/hbhi-nigeria-publication-2021 commit dcc2f01 (2023-05-10): "
               "simulation/analyzer/data/LGA_funder.csv (2020 funder split, NMEP), simulation_inputs/"
               "LGA_and_respective_archetype.csv (22 archetypes), DS_vector_rel_abundance.csv (species shares), "
               "SMC scenario lists (historical 2013-2019; BAU, NMSP 2020 404-LGA, budget-prioritised 235 and "
               "310 plans), historical ITN_by_LGA.csv (pyrethroid bioassay mortality), future ITN "
               "itn_scenario3_4_funded_2020_2030.csv (2020-plan campaign cohort and net type), ACT "
               "HS_by_LGA_git.csv (U5 effective case-management coverage, 2018)",
        citation="Ozodiegwu ID et al. 2023, Application of mathematical modelling to inform national malaria "
                 "intervention planning in Nigeria, Malaria Journal 22:137",
        licence="repository Apache-2.0; its inputs derive from NMEP plans, DHS and MAP (their terms apply)",
        processing="LGA names normalised (lower-case alphanumerics) for the join, 774/774 matched; archetype_id "
                   "= rank of the archetype's representative LGA name; SMC lists as 0/1 flags; first row per LGA "
                   "for bioassay mortality; first planned year and its net type for the 2020 plan"),
    "lga_rainfall_nasapower.csv": dict(
        rows="309 LGAs", from_="lga_rainfall_seasonality_nasapower.csv",
        source="NASA POWER climatology API, PRECTOTCORR (power.larc.nasa.gov/api/temporal/climatology/point), "
               "one call per LGA representative point",
        citation="NASA Langley Research Center POWER Project, funded through the NASA Earth Science Directorate "
                 "Applied Science Program",
        licence="no formal licence; POWER requests citation and notification when data are passed to others",
        processing="monthly mean mm/day; annual mm; Walsh-Lawler seasonality index; peak month; months >= 100 mm"),
    "lga_itn_campaign_calendar.csv": dict(
        rows="campaign rows for the 309 LGAs", from_="itn_campaign_calendar_lga.csv",
        source="hbhi-nigeria-publication-2021 historical ITN inputs (2010-2018 estimates) and the "
               "budget-prioritised 2020-2030 plan (planned rows, not observations)",
        citation="Ozodiegwu et al. 2023 (above)",
        licence="Apache-2.0 repository (see above)",
        processing="one row per LGA and campaign; the `status` column separates historical estimates from "
                   "PLANNED rows; assumed_* columns are the hbhi model inputs (use, kill, blocking); historical "
                   "net type assumed pyrethroid"),
    "state_funder_map.csv": dict(
        rows="37 states (national context: defines the 13 Global Fund states)", from_="state_funder_map.csv",
        source="hbhi LGA_funder.csv aggregated by state (NMEP 2020 donor split)",
        citation="Ozodiegwu et al. 2023 (above)", licence="Apache-2.0 repository (see above)",
        processing="funder is constant within each state; LGA count and WorldPop 2024 population per state"),
    "dhs_malaria_indicators_long.csv": dict(
        rows="13 states, their 4 zones and national; surveys NG2010MIS, NG2013DHS, NG2015MIS, NG2018DHS, "
             "NG2021MIS, NG2024DHS", from_="dhs_nigeria_subnational_malaria_indicators_long.csv",
        source="DHS Program API (api.dhsprogram.com/rest/dhs/data, 27 ML_ indicators, breakdown subnational and "
               "national), downloaded 2026-10-28",
        citation="ICF, The DHS Program STATcompiler/API; National Malaria Elimination Programme (NMEP), National "
                 "Population Commission (NPC) and ICF: Nigeria MIS 2010, 2015, 2021 and Nigeria DHS 2013, 2018, 2024",
        licence="DHS Program terms of use (redistribution not clearly permitted): not for the public repository",
        processing="'..' stripped; 'FCT Abuja' -> 'Federal Capital Territory'; zone from the fixed state-zone "
                   "list; value_unit = count when the indicator starts 'Number of', else percent; no values altered"),
    "state_pfpr_survey_targets.csv": dict(
        rows="13 states and national", from_="state_pfpr_calibration_targets.csv",
        source="the DHS rows above, indicators ML_PMAL_C_MSY (microscopy) and ML_PMAL_C_RDT, children 6-59 months",
        citation="as dhs_malaria_indicators_long.csv", licence="as dhs_malaria_indicators_long.csv",
        processing="pivot: value, 95% CI and weighted / unweighted denominators per survey and state"),
    "state_itn_cm_coverage_by_survey.csv": dict(
        rows="13 states and national", from_="state_itn_cm_coverage_by_survey.csv",
        source="the DHS rows above: ITN access/use/ownership, fever care-seeking, testing, ACT use, IRS, IPTp",
        citation="as dhs_malaria_indicators_long.csv", licence="as dhs_malaria_indicators_long.csv",
        processing="pivot to one row per survey and state; denominators from the _UNW / _NUM indicators"),
    "state_smc_itn_history_2020_2025.csv": dict(
        rows="rows for the 13 states", from_="state_smc_itn_history_2020_2025.csv",
        source="compiled by hand from AMP/NMEP Nigeria campaign sitreps 2022-2025 "
               "(allianceformalariaprevention.com), Malaria Consortium pages, and PMC9835293, PMC10088202, "
               "PMC10161462 (citations S1-S13 in citations_a8.txt)",
        citation="see citations_a8.txt S1-S13", licence="facts compiled from public reports and open-access papers",
        processing="hand-entered from the cited tables; no imputation; inferred rows flagged in evidence_strength"),
    "gf_nigeria_malaria_envelopes.csv": dict(
        rows="national", from_="gf_nigeria_malaria_envelopes.csv",
        source="Global Fund Data Explorer API (data.api.theglobalfund.org allocations and budgets, NGA, Malaria)",
        citation="The Global Fund Data Explorer, accessed 2026-10-28 (C13, C14 in citations_a8.txt)",
        licence="Global Fund published data; terms not verified (page unavailable 2026-11-01)",
        processing="JSON to CSV"),
    "gf_gc7_nga_malaria_budget_by_activity_area.csv": dict(
        rows="national, 28 activity areas", from_="gf_gc7_nga_malaria_budget_by_activity_area.csv",
        source="Global Fund OData v4.2 allFinancialIndicators (fetch.theglobalfund.org), GC7 NGA malaria grant "
               "budgets grouped by activity area",
        citation="The Global Fund Data Explorer (C58 in citations_a8.txt)",
        licence="as gf_nigeria_malaria_envelopes.csv",
        processing="JSON to CSV, sorted by amount, share of total added"),
    "gf_gc7_nga_cm_smc_itn_budget_by_cost_category.csv": dict(
        rows="national", from_="gf_gc7_nga_cm_smc_itn_budget_by_cost_category.csv",
        source="as above, activity area x financial category, for SMC, iCCM, facility-based treatment, "
               "private-sector CM and ITN mass campaign",
        citation="as above", licence="as gf_nigeria_malaria_envelopes.csv",
        processing="JSON to CSV, share within each activity area"),
    "unit_costs_compiled_from_literature.csv": dict(
        rows="47 cost rows", from_="unit_costs_compiled_from_literature.csv",
        source="values transcribed from published papers and Global Fund reference price lists (ITN PPM lists "
               "Oct 2023, Q2 2025, 2026; IRS reference prices) with the citation in each row",
        citation="per row (C-ids resolved in citations_a8.txt)",
        licence="facts from published sources",
        processing="transcribed; value_usd_2024 = value x US CPI-U ratio (2024 = 313.7) by price year"),
    "itn_supply_by_type.csv": dict(
        rows="SSA and Nigeria, 2022-2024", from_="itn_supply_by_type.csv",
        source="WHO World Malaria Report 2024 (s7.1) and 2025 (s5.1.4), WHO IRIS",
        citation="WHO, World Malaria Report 2024 (hdl 10665/379751) and 2025 (hdl 10665/383951)",
        licence="WHO publications CC BY-NC-SA 3.0 IGO", processing="numbers transcribed from the text"),
    "cost_parameters_a8_registry.yaml": dict(
        rows="cost, envelope and coverage entries", from_="citations.md '## Parameter Registry' block",
        source="attempt 8's parameter registry: unit costs, PSM share, persons per net, CM floor (GC7 budget "
               "lines kept as is), the question's envelope and post-campaign ITN use; effect-size and helper "
               "entries left out",
        citation="per entry (`source`, resolved in citations_a8.txt)", licence="facts from published sources",
        processing="entries copied unchanged except code_refs (attempt 8 code locations) removed"),
    "citations_a8.txt": dict(
        rows="the citation entries the files above name", from_="citations.md",
        source="attempt 8's citation index (each entry: value, source, URL verified, verbatim quote)",
        citation="-", licence="quotations from published sources",
        processing="the ## [Cnn] / ### [Snn] sections referenced by the other files, copied unchanged"),
}

LGA_GEO = ["lga_id", "state", "lga", "district_code", "area_km2", "lon", "lat", "pop_worldpop_2024",
           "pop_u5_worldpop_2024", "pop_5_14_worldpop_2024", "pop_15plus_worldpop_2024"]
LGA_HBHI = ["funder_2020_hbhi", "archetype_repDS", "archetype_id", "arabiensis_rel_abundance", "gambiae_rel_abundance",
            "funestus_rel_abundance", "smc_hist_ever_2013_2019", "smc_nmsp2020_404", "smc_budget_plan_235",
            "smc_plan_310", "smc_bau_2020_2030_80", "pyrethroid_bioassay_mortality_hbhi",
            "itn_cohort_first_year_2020plan", "itn_net_type_2020plan", "cm_u5_effective_cov_2018_hbhi"]
REGISTRY_KINDS = ("cost_usd", "coverage")
REGISTRY_NAMES = ("psm_freight_share", "persons_per_net")      # cost-model inputs of other kinds
REGISTRY_DROP = re.compile(r"_z_plus5$")


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _registry(citations_md: str) -> list[dict]:
    import yaml
    t = open(citations_md, encoding="utf-8").read()
    i = t.find("## Parameter Registry")
    m = re.search(r"```ya?ml\n(.*?)```", t[i:], re.S) if i >= 0 else None
    if not m:
        raise SystemExit(f"{citations_md}: no Parameter Registry yaml block")
    out = []
    for e in yaml.safe_load(m.group(1))["parameters"]:
        if (e.get("kind") in REGISTRY_KINDS or e["name"] in REGISTRY_NAMES) and not REGISTRY_DROP.search(e["name"]):
            out.append({k: v for k, v in e.items() if k != "code_refs"})
    return out


def _citation_sections(citations_md: str, ids: set[str]) -> str:
    """The `## [Cnn]` / `### [Snn]` sections whose leading id is in `ids`, in file order."""
    lines = open(citations_md, encoding="utf-8").read().splitlines()
    out, keep = [], False
    for ln in lines:
        m = re.match(r"^(#{2,3}) \[([CS]\d+)[\]/]", ln)      # not "[S11 use, ...]" (a run note)
        if ln.startswith("## ") or ln.startswith("### "):
            keep = bool(m and m.group(2) in ids)
        if keep:
            out.append(ln)
    return "\n".join(out) + "\n"


def _ids_in(text: str) -> set[str]:
    ids = set(re.findall(r"\b([CS]\d{1,2})\b", text))
    for a, b in re.findall(r"\bS(\d+)\s*-\s*S(\d+)\b", text):       # "S1-S12"
        ids |= {f"S{k}" for k in range(int(a), int(b) + 1)}
    return ids


def build(run_dir: str, out: str) -> dict:
    import pandas as pd
    import yaml
    dp = os.path.join(run_dir, "data", "processed")
    os.makedirs(out, exist_ok=True)
    m = pd.read_csv(os.path.join(dp, "lga_master_774.csv"))
    fm = pd.read_csv(os.path.join(dp, "state_funder_map.csv"))
    states = sorted(fm.loc[fm.funder_2020_hbhi == GF, "state"])
    g = m[m.state.isin(states)].sort_values("lga_id")
    assert len(states) == 13 and len(g) == 309, (len(states), len(g))
    key = ["lga_id", "state", "lga"]
    w = lambda df, name: df.to_csv(os.path.join(out, name), index=False)   # noqa: E731
    w(g[LGA_GEO], "lga_geography_population.csv")
    w(g[key + [c for c in g.columns if c.startswith("map_")]], "lga_map_estimates.csv")
    w(g[key + LGA_HBHI], "lga_hbhi_attributes.csv")
    r = pd.read_csv(os.path.join(dp, "lga_rainfall_seasonality_nasapower.csv"))
    r = g[key].merge(r, on=["state", "lga"], how="left", validate="1:1")
    assert r.drop(columns=key).notna().all().all(), "rainfall join"
    w(r, "lga_rainfall_nasapower.csv")
    c = pd.read_csv(os.path.join(dp, "itn_campaign_calendar_lga.csv"))
    c = c[c.state.isin(states)]
    assert set(zip(c.state, c.lga)) == set(zip(g.state, g.lga)), "calendar LGAs"
    w(c, "lga_itn_campaign_calendar.csv")
    w(fm, "state_funder_map.csv")
    zones = set(pd.read_csv(os.path.join(dp, "dhs_nigeria_subnational_malaria_indicators_long.csv"))
                .query("level == 'state' and unit in @states").zone)
    d = pd.read_csv(os.path.join(dp, "dhs_nigeria_subnational_malaria_indicators_long.csv"))
    d = d[(d.level == "national") | ((d.level == "state") & d.unit.isin(states)) | ((d.level == "zone") & d.unit.isin(zones))]
    w(d, "dhs_malaria_indicators_long.csv")
    for src, name in (("state_pfpr_calibration_targets.csv", "state_pfpr_survey_targets.csv"),
                      ("state_itn_cm_coverage_by_survey.csv", "state_itn_cm_coverage_by_survey.csv")):
        t = pd.read_csv(os.path.join(dp, src))
        unit = t.unit.replace({"FCT Abuja": "Federal Capital Territory"})
        t = t[(t.level == "national") | ((t.level == "state") & unit.isin(states))]
        assert set(t[t.level == "state"].unit) <= set(states)
        w(t, name)
    h = pd.read_csv(os.path.join(dp, "state_smc_itn_history_2020_2025.csv"))
    w(h[h.state.isin(states)], "state_smc_itn_history_2020_2025.csv")
    for name in ("gf_nigeria_malaria_envelopes.csv", "gf_gc7_nga_malaria_budget_by_activity_area.csv",
                 "gf_gc7_nga_cm_smc_itn_budget_by_cost_category.csv", "unit_costs_compiled_from_literature.csv",
                 "itn_supply_by_type.csv"):
        w(pd.read_csv(os.path.join(dp, name)), name)
    cit = os.path.join(run_dir, "citations.md")
    reg = _registry(cit)
    with open(os.path.join(out, "cost_parameters_a8_registry.yaml"), "w") as f:
        f.write("# Attempt 8 parameter registry: cost, envelope and coverage entries (see README.txt)\n")
        yaml.safe_dump({"parameters": reg}, f, sort_keys=False, width=110)
    ids = set()
    for name in ("unit_costs_compiled_from_literature.csv", "itn_supply_by_type.csv", "cost_parameters_a8_registry.yaml",
                 "state_smc_itn_history_2020_2025.csv", "gf_nigeria_malaria_envelopes.csv"):
        ids |= _ids_in(open(os.path.join(out, name), encoding="utf-8").read())
    ids |= {"C13", "C14", "C38", "C58"}
    ids -= {"C5"}          # "C5 CM floor" names attempt 8's brief constraint C5, not citation C5 (scenario results)
    with open(os.path.join(out, "citations_a8.txt"), "w", encoding="utf-8") as f:
        f.write("# Citation entries from E7 attempt 8's citations.md that the files in this folder name.\n\n")
        f.write(_citation_sections(cit, ids))
    write_readme(out, states)
    return write_manifest(out, run_dir)


def write_readme(out: str, states: list[str]) -> None:
    L = ["Inputs: Nigeria malaria allocation, 13 Global Fund-supported states (v2 run)",
         "=" * 76, "",
         f"States ({len(states)}, funder 'Global Fund' in the NMEP 2020 donor split, state_funder_map.csv): "
         + ", ".join(states) + ". 309 LGAs.",
         "", "Every file was produced from E7 attempt 8's downloads (2026-10-28/29) and processed tables;",
         "the original source, citation, licence and processing of each file are below. Keys: lga_id (1-774,",
         "attempt 8's LGA index), state, lga. Nothing here is a model output.", ""]
    for name, s in SOURCES.items():
        L += [name, "-" * len(name), f"  rows:       {s['rows']}", f"  from:       {A8}{s['from_']}",
              f"  source:     {s['source']}", f"  citation:   {s['citation']}", f"  licence:    {s['licence']}",
              f"  processing: {s['processing']}", ""]
    L += ["manifest.json: sha256 and size of every file (written last).", ""]
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def write_manifest(out: str, run_dir: str | None = None) -> dict:
    files = sorted(n for n in os.listdir(out) if n != MANIFEST and os.path.isfile(os.path.join(out, n)))
    man = {"name": "nigeria_gf13_v2", "storage_prefix": STORAGE_PREFIX, "tree_dir": TREE_DIR,
           "built_from": run_dir, "files": {n: {"sha256": sha256(os.path.join(out, n)),
                                               "bytes": os.path.getsize(os.path.join(out, n))} for n in files}}
    with open(os.path.join(out, MANIFEST), "w") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    return man


def verify(d: str) -> list[str]:
    try:
        man = json.load(open(os.path.join(d, MANIFEST)))
    except (OSError, ValueError) as e:
        return [f"{MANIFEST}: {e}"]
    bad = []
    for n, e in man.get("files", {}).items():
        p = os.path.join(d, n)
        if not os.path.isfile(p) or os.path.islink(p):
            bad.append(f"{n}: missing")
        elif sha256(p) != e["sha256"]:
            bad.append(f"{n}: sha256 differs from the manifest")
    extra = sorted(set(os.listdir(d)) - set(man.get("files", {})) - {MANIFEST})
    bad += [f"{n}: not in the manifest" for n in extra]
    if not man.get("files"):
        bad.append("manifest lists no files")
    return bad


def question(d: str) -> str:
    man = json.load(open(os.path.join(d, MANIFEST)))
    names = ["README.txt"] + sorted(n for n in man["files"] if n != "README.txt")
    paths = ", ".join(f"{TREE_DIR}/{n}" for n in names)
    # No full stop after the last path: the harness's tokenizer would read "x.csv." as a non-input token.
    return f"{QUESTION_E7} {SCOPE} Input files (sources and processing in {TREE_DIR}/README.txt): {paths}"


def _store():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import results_store
    return results_store


def upload(d: str, account: str | None) -> str:
    bad = verify(d)
    if bad:
        raise SystemExit("refusing to upload: " + "; ".join(bad))
    rs = _store()
    man = json.load(open(os.path.join(d, MANIFEST)))
    for n in sorted(man["files"]):
        rs.upload_retry(os.path.join(d, n), f"{STORAGE_PREFIX}/{n}", account)
    rs.upload_retry(os.path.join(d, MANIFEST), f"{STORAGE_PREFIX}/{MANIFEST}", account)
    return sha256(os.path.join(d, MANIFEST))


def fetch(dest: str, account: str | None) -> list[str]:
    rs = _store()
    os.makedirs(dest, exist_ok=True)
    rs.download(f"{STORAGE_PREFIX}/{MANIFEST}", os.path.join(dest, MANIFEST), account)
    for n in json.load(open(os.path.join(dest, MANIFEST)))["files"]:
        if os.path.basename(n) != n:
            raise SystemExit(f"manifest names a path, not a file: {n}")
        rs.download(f"{STORAGE_PREFIX}/{n}", os.path.join(dest, n), account)
    return verify(dest)


def stage_tree(d: str, tree: str) -> str:
    """Copy the inputs into <tree>/data/nigeria_gf13/ and commit them on a local branch that cannot be pushed."""
    bad = verify(d)
    if bad:
        raise SystemExit("refusing: " + "; ".join(bad))
    git = lambda *a: subprocess.check_output(["git", "-C", tree, *a], text=True).strip()   # noqa: E731
    if git("status", "--porcelain"):
        raise SystemExit(f"{tree} has uncommitted changes")
    for remote in git("remote").split():
        git("remote", "set-url", "--push", remote, "DISABLED-local-inputs-never-pushed")
    git("config", "branch.autoSetupMerge", "false")
    git("checkout", "-q", "-B", LOCAL_BRANCH)
    dest = os.path.join(tree, TREE_DIR)
    os.makedirs(dest, exist_ok=True)
    man = json.load(open(os.path.join(d, MANIFEST)))
    for n in list(man["files"]) + [MANIFEST]:
        with open(os.path.join(d, n), "rb") as src, open(os.path.join(dest, n), "wb") as dst:
            dst.write(src.read())
    git("add", "-f", TREE_DIR)
    git("commit", "-q", "-m", "LOCAL ONLY (never push): Nigeria GF13 inputs for the v2 launch")
    return git("rev-parse", "HEAD")


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, msg):
        if not c:
            fails.append(msg)
    with tempfile.TemporaryDirectory() as td:
        cit = os.path.join(td, "citations.md")
        open(cit, "w").write("# x\n## [C11] use\n- v\n## [C12] other\n- w\n### [S3] s3\n- z\n"
                             "## [S11 use, R-041] note\n- n\n## Parameter Registry\n```yaml\nparameters:\n"
                             "- {name: a, kind: cost_usd, value: 1, source: C11, code_refs: [x]}\n"
                             "- {name: cost_smc_z_plus5, kind: dimensionless, value: 5}\n"
                             "- {name: b_z_plus5, kind: cost_usd, value: 5}\n- {name: eff, kind: odds_ratio, value: 0.3}\n```\n")
        reg = _registry(cit)
        ok([e["name"] for e in reg] == ["a"] and "code_refs" not in reg[0],
           f"NG1 the registry keeps cost/coverage entries, drops helpers, effect sizes and code_refs: {reg}")
        txt = _citation_sections(cit, {"C11", "S3"})
        ok("[C11]" in txt and "[S3]" in txt and "[C12]" not in txt and "Parameter" not in txt and "S11" not in txt,
           f"NG2 only the named citation sections are copied: {txt!r}")
        ok(_ids_in("see S1-S3; C52 and C5") >= {"S1", "S2", "S3", "C52", "C5"}, "NG3 ids and S ranges are found")
        d = os.path.join(td, "in")
        os.makedirs(d)
        open(os.path.join(d, "a.csv"), "w").write("x\n1\n")
        open(os.path.join(d, "README.txt"), "w").write("r\n")
        write_manifest(d)
        ok(verify(d) == [], f"NG4 a fresh manifest verifies: {verify(d)}")
        open(os.path.join(d, "a.csv"), "a").write("2\n")
        ok(any("sha256" in x for x in verify(d)), "NG5 a changed file fails verification")
        open(os.path.join(d, "b.csv"), "w").write("y\n")
        ok(any("not in the manifest" in x for x in verify(d)), "NG6 an extra file fails verification")
        write_manifest(d)
        q = question(d)
        ok(q.startswith(QUESTION_E7) and SCOPE in q and f"{TREE_DIR}/a.csv" in q and f"{TREE_DIR}/README.txt" in q,
           "NG7 the question is the original text, the scope sentence and the input paths")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from agents.stage_machine import _question_paths
        ok(set(_question_paths(q)) >= {f"{TREE_DIR}/a.csv", f"{TREE_DIR}/b.csv", f"{TREE_DIR}/README.txt"},
           f"NG8 the harness finds every input path in the question: {_question_paths(q)}")
        tree = os.path.join(td, "tree")
        subprocess.run(["git", "init", "-q", tree], check=True)
        subprocess.run(["git", "-C", tree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "i"], check=True)
        subprocess.run(["git", "-C", tree, "remote", "add", "origin", "https://example.invalid/r.git"], check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        old = dict(os.environ)
        os.environ.update(env)
        try:
            stage_tree(d, tree)
        finally:
            os.environ.clear()
            os.environ.update(old)
        files = subprocess.check_output(["git", "-C", tree, "ls-files"], text=True).split()
        push = subprocess.check_output(["git", "-C", tree, "remote", "get-url", "--push", "origin"], text=True).strip()
        ok(f"{TREE_DIR}/a.csv" in files and f"{TREE_DIR}/{MANIFEST}" in files and push.startswith("DISABLED"),
           f"NG9 stage-tree commits the inputs locally with pushes disabled: {files} {push}")
    if fails:
        print("SELF-TEST FAILURES:", file=sys.stderr)
        for f_ in fails:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("run_dir")
    b.add_argument("out")
    for name in ("verify", "question"):
        sub.add_parser(name).add_argument("dir")
    u = sub.add_parser("upload")
    u.add_argument("dir")
    u.add_argument("--account")
    f = sub.add_parser("fetch")
    f.add_argument("dest")
    f.add_argument("--account")
    s = sub.add_parser("stage-tree")
    s.add_argument("dir")
    s.add_argument("tree")
    a = ap.parse_args()
    if a.cmd == "build":
        man = build(a.run_dir, a.out)
        print(f"wrote {len(man['files'])} files to {a.out}; manifest sha256 {sha256(os.path.join(a.out, MANIFEST))}")
    elif a.cmd == "verify":
        bad = verify(a.dir)
        print("\n".join(bad) or "OK")
        return 1 if bad else 0
    elif a.cmd == "question":
        print(question(a.dir))
    elif a.cmd == "upload":
        print(f"uploaded to {STORAGE_PREFIX}/; manifest sha256 {upload(a.dir, a.account)}")
    elif a.cmd == "fetch":
        bad = fetch(a.dest, a.account)
        print("\n".join(bad) or f"OK: {a.dest}")
        return 1 if bad else 0
    elif a.cmd == "stage-tree":
        print(f"{a.tree}: local commit {stage_tree(a.dir, a.tree)} on {LOCAL_BRANCH} (pushes disabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
