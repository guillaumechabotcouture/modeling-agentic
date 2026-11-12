---
phase: "F6"
commit: "S2-BG4"
status_group: "F6 Stage 2"
---

## Changelog

**A VM still being provisioned is priced at the highest bound instead of refusing (F6 S2-BG4).** On 2026-11-11 at 21:17 the canary's cluster 2366111 was scaling from 5 to 48 workers. `budget.job_meter` raised "VM type None is not in cloud/vm_prices.yaml" three readings in a row, about 5 minutes, because VMs that are still being provisioned report `instance_type_id` None. `launch_watch` then stopped the run as spend-unreadable, by design. An instance with no VM type is now priced at the highest per-hour bound in `cloud/vm_prices.yaml`, the conservative choice. The cluster's row in the breakdown states this as an assumption, and so does `budget.py status`. Once the type appears, its own bound applies. A named VM type that is not in the table still refuses (fail closed). Self-tests were written first and failed before the change. BR21: a scaling cluster with untyped live VMs gives a number, at least that of the same cluster with typed VMs, and the job meter reads it. BR21b: an unlisted named type refuses.

## Status

- **S2-BG4:** VMs with no type yet no longer make spend unreadable; to be seen on the next scaling run.
