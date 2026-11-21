---
phase: "harness benchmark"
commit: "eval-run-no-key α-β"
---

## Changelog

**The eval launch preflight no longer opens the answer key (owner decision, 2026-11-21).** `scripts/eval_suite.py
run` used to decrypt the encrypted hidden half (keychain key) to regenerate the inputs and read the answer keys'
input hashes, so only the owner could run it. It now calls `eval_private.verify_public`: the pinned ciphertext and the
public manifest are downloaded and checked by digest (ciphertext against the pin and the manifest, the manifest's
other fields, and the manifest's own sha256 when the pin records it), and the shipped tree's archived inputs must hash
to the manifest's input hashes. It never decrypts and never reads the keychain, and takes no `--private-dir`.
`fetch` (grade and the maintainer commands) shares the same checks before it decrypts. `publish` now also pins the
manifest's sha256 in `evals/private.yaml`; a pin without it is still accepted, since grading re-checks the manifest's
input hashes against the decrypted answer keys. Self-tests: E10 (`verify_public` never decrypts or reads the
keychain; wrong bundle digest, wrong manifest digest and a missing pin fail closed), S20 (`run` prints its launch
lines with the decrypt, keychain, fetch and open functions all refusing; a wrong bundle digest, a missing bundle, a
`--private-dir` and shipped inputs that differ from the manifest each refuse before any launch line; the launcher
sources never reference the bundle's key or plaintext).
