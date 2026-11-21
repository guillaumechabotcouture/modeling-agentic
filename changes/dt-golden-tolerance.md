---
phase: "v2 simplification"
commit: "S1b"
---

## Changelog

**The golden check of decision tests works across machines (simplification pass S1b).** #262's check compared strict digests, and CI runners with different BLAS kernels give different late digits (measured 2026-11-20: 3,273 of 16,114 numbers differ at 10 significant digits, the largest by 3.2e-05 relative), so it passed or failed with the runner. `toolkit/analysis/dt_golden.py` now splits each record: everything but the floating-point values (plan digests, verdicts, recommendations, whole-number counts, words) is the exact part and must be equal; the floating-point values must agree within `RTOL` 3e-4 relative (10 times the largest measured difference) and `ATOL` 1e-6 absolute. `dt_golden.json` holds the numbers and the platform it was recorded on. A failed check prints one line per record, and `dt_golden.py record <log> --source <where>` rebuilds the file from such a log, refusing a log that is cut short, holds two captures or shows another failed case. `digest()` and `digest <run_dir>` are unchanged. Known limit: two case labels closer than the tolerance are not told apart. Tests DG23 and DG24, written first.
