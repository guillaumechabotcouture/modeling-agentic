---
phase: "harness benchmark"
commit: "eval-run-no-key α-δ"
---

## Changelog

**The eval launch preflight no longer opens the answer key (owner decision, 2026-11-21).** `scripts/eval_suite.py
run` used to decrypt the encrypted hidden half (keychain key) to regenerate the inputs and read the answer keys'
input hashes, so only the owner could run it. It now calls `eval_private.verify_public`: the pinned ciphertext and the
public manifest are downloaded and checked by digest (ciphertext against the pin and the manifest, the manifest's
other fields, and the manifest's own sha256 when the pin records it), and the shipped tree's archived inputs must hash
to the manifest's input hashes. It never decrypts and never reads the keychain, and takes no `--private-dir`.
`fetch` (grade and the maintainer commands) shares the same checks before it decrypts. `publish` now also pins the
manifest's sha256 in `evals/private.yaml`; a pin without it is still accepted with a printed note, since grading
re-checks the manifest's input hashes against the decrypted answer keys, while a present but invalid one refuses.
Since `run` now trusts the manifest's hashes, `publish` also refuses a stored answer key whose input hashes differ
from the committed inputs (`stale_answer_keys`). `run` also refuses an inherited `EVAL_TREE` without `--tree` (the
printed command would launch a tree the preflight never checked) and a tag outside a-z, 0-9 and - (it reaches a
shell under `--execute`); `write_pin` refuses a pin without its required fields; `--self-test` takes no command.
Self-tests: E10 (`verify_public` never decrypts or reads the
keychain; wrong bundle digest, wrong manifest digest and a missing pin fail closed), S20 (`run` prints its launch
lines with the decrypt, keychain, fetch and open functions all refusing; a wrong bundle digest, a missing bundle, a
`--private-dir` and shipped inputs that differ from the manifest each refuse before any launch line; the launcher
sources never reference the bundle's key or plaintext).
