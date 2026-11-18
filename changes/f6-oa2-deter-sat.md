---
phase: "F6"
commit: "OA2 α–δ"
---

## Changelog

**Malaria engine v0.18: how much deterrence a PBO net keeps (option A, step 2; owner 2026-11-18, form P).** New engine parameter `net_deter_sat` θ in [0, 1] (kind fixed, units share, default 1), acting on PBO nets only (`vector.hut_new_net`): a PBO net's entry becomes m_pyr + θ (m_pbo − m_pyr), with m_pyr a pyrethroid net's entry at the same bioassay mortality and kill shift; hut mortality and decay stay the PBO net's. θ = 1 is v0.17 bit for bit (V38, against the frozen v0.16 code); θ = 0 lets a PBO net at x = 0.55 kill 0.58 per attempt instead of 0.11. Pyrethroid and dual-AI nets are unchanged. Card prior uniform 0-1 (`priors.yaml`); `decision_inputs.yaml` and the B9d-B9g coverage rows name it from v0.18; card.yaml regenerated; ENGINE_NOTES.md v0.18. The engine identity changes, so the stored evidence is refused until `benchmarks/restamp_proof.py prove` (run on Coiled; new `--old-tar`/`--old-commit`/`--new-commit` options for a tree without git) shows v0.17 and v0.18 identical and `restamp` re-stamps it. Self-tests V38, V39, NE4, RP-T1, RP-T2 (a re-stamp needs a full-scope proof whose old side is the stored evidence; unknown --settings refused; Codex review); V31, NE0 and PT6 updated.
