---
phase: "F6"
commit: "S2-BG4"
status_group: "F6 Stage 2"
---

## Changelog

**VMs with no type yet, and VMs Azure never created, no longer make spend unreadable (F6 S2-BG4).** On 2026-11-11 at 21:17 the canary's cluster 2366111 was scaling from 5 to 48 workers. `budget.job_meter` raised "VM type None is not in cloud/vm_prices.yaml" three readings in a row, about 5 minutes, and `launch_watch` stopped the run as spend-unreadable, by design. Seven of its workers were in `error` with `instance_type_id` None and the reason "Failed to create VM -- (OperationNotAllowed) ... exceeding approved standard ... quota". An ended VM record with no type that never ran counts $0 and is listed ("N instance(s) failed to create, counted $0"). Never ran means its reason starts "Failed to create", or no time passed between its creation and its last state. A VM still being provisioned also has no type yet; it is priced conservatively, in three cases. A worker takes the highest bound among the VM types the same cluster's other workers have; the small scheduler type would undercount worker VMs. A scheduler takes the highest bound among the scope's recent clusters' schedulers. Either takes the table's highest bound when nothing comparable is typed. The cluster's row in the breakdown and `budget.py status` state these as assumptions. Once the type appears, its own bound applies. A named VM type that is not in the table still refuses (fail closed). Self-tests were written first and failed before each change. BR21 prices untyped pending workers at the other workers' type. BR21c falls back to the table maximum when no worker is typed. BR21d covers the scheduler rule. BR21e uses cluster 2366111's record shape: seven failed-to-create workers count $0 and are listed. BR21b: an unlisted named type refuses.

## Status

- **S2-BG4:** VMs with no type yet no longer make spend unreadable, and VMs that failed to create count $0; to be seen on the next scaling run.
