---
phase: "F6"
commit: "WM-F α"
---

## Changelog

**The world model in every v2 run: PREREGISTER and SCORE.** Two stage-machine states (docs/design/v2c-orchestration.md §14, `agents/world_model_stage.py`). PREREGISTER (after ENGINE_CHOICE, before CONFIGURE) runs the seed task once per run in `world_model/seed/` on topics from the signed spec (decision tests, plan variants, departure from the comparator, value of information, component shares), each with fixed claims; masks the input strictly and checks it whole; writes the outcome map; checks both files with `decision_beliefs.publish_problems`; anchors them on `wm-trials` (`GitAnchor`, `reachable_at`), or records a local-only anchor marked not pre-registered. SCORE (after ANALYZE, again before CRITIQUE and WRITE when the analysis changed) scores each belief by code, updates the belief table and writes `world_model/world_model.md`, which the critics read first and `render_report` appends. Fail soft: no failure blocks or changes the analysis. Self-tests W1-W13 (W8b, W10b).
