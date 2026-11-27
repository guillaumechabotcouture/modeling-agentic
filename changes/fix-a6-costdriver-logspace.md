---
phase: "Agent-built engines (#363 follow-up: A6 active-mode cost drivers & log-space multi-decade sampling)"
commit: "evaluate non-decision-input cost_driver effects under active component modes and sample multi-decade positive continuous attributes in log space during admission"
---

## Changelog

- `toolkit/engines/built/admission.py`:
  - Sample continuous unit attributes spanning multiple orders of magnitude (`lo > 0` and `hi / lo >= 50`) in log space in `_sample_attr()` and `_domain_points_attr()`, preventing linear Latin-hypercube draws from concentrating base points in an extreme saturation tail.
  - In `check_a6_a7()`, when an input outside `decision_inputs` declares an expected effect on a `cost_driver`, evaluate it under active modes of components affecting that cost driver in addition to the all-absent package (where intervention cost drivers are legitimately zero), while still failing dead/unwired inputs and enforcing declared signs.
  - Add self-test `BE_A6_COST_LOG` covering both behaviors.
