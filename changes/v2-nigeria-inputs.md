---
phase: "v2"
commit: "N1"
---

## Changelog

**Inputs and launch config for the v2 Nigeria run (13 Global Fund states), no data in git.** `cloud/nigeria_gf13_inputs.py` builds 18 source-level files from attempt 8's downloads and processed tables (LGA WorldPop populations by age, MAP 202608 LGA estimates, hbhi LGA attributes, NASA POWER rainfall, ITN campaign calendar, DHS/NMIS state indicators and PfPR, state SMC/ITN history, GF GC7 envelopes and budget lines, unit costs, ITN supply, attempt 8's cost registry entries and the citations they name), with a README.txt of sources, licences and processing and a sha256 manifest. It also uploads them to storage (`inputs/nigeria_gf13_v2/`), fetches them back with sha256 checks, stages them into a launch tree as a local-only commit with pushes disabled, and prints the v2 question (the original question, the owner's scope sentence, the input paths). DHS terms do not clearly allow redistribution, so `/data/nigeria_gf13/` is gitignored. `cloud/configs/nigeria_gf13_v2.py` is the launch config (not launched; it refuses to launch until the inputs are staged in its tree). The self-test covers the registry filter, the citation extract, manifest checks, harness path parsing of the question and the local-only commit.
