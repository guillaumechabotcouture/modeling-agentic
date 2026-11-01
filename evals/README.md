# Eval suite: public half

This repository is public, so it holds only the public half of the eval suite:

- `evals/<q>/question.txt`: the question text a run receives;
- `data/evals/<q>/`: the synthetic input data, which ship with the code;
- `evals/suite.yaml` (the question list), `evals/taxonomy.yaml` (failure codes and layers)
  and `evals/private.yaml` (which bundle version to fetch, and its ciphertext sha256);
- the generic grader, `scripts/eval_suite.py`, and `cloud/configs/eval_<q>.py`.

The hidden half reveals the answers, so it is not in the repository. It holds each
question's generator, rubric and answer key, the question-specific tests and the design
note. It lives in the IDM Coiled storage account as one encrypted bundle:

    outputs/evals_private/v<N>/bundle.enc       ciphertext (Fernet)
    outputs/evals_private/v<N>/manifest.json    sha256 of the ciphertext, input hashes

Run VMs can read that storage account, so only ciphertext is stored there. The key is
kept in the maintainer's macOS login keychain (service `modeling-agentic-evals`, account
`evals`) and nowhere else.

## Using it

You need `az login`, `$COILED_STORAGE_ACCOUNT` set to the IDM Coiled workspace's storage
account, and the keychain item. Commands that need the hidden half fetch and decrypt it
into a temporary directory, and delete that directory when they finish:

    python3 scripts/eval_suite.py grade <q> <run_dir> [--unverified-inputs]
    python3 scripts/eval_suite.py grade --root ~/modeling-agentic-keep/evals/<tag>
    python3 scripts/eval_suite.py run [--tag T] [q ...]
    python3 scripts/eval_suite.py generate --check
    python3 scripts/eval_suite.py --self-test --with-private

The grader picks the answer key whose input hashes match the run's. That lets a run
made on earlier inputs keep its legacy key (`legacy/<name>` in the bundle).

## Changing a question (maintainer)

    python3 scripts/eval_suite.py fetch ~/eval-private-edit      # plaintext; outside any checkout
    # edit generators or rubrics there, then:
    python3 scripts/eval_suite.py generate --private-dir ~/eval-private-edit
    python3 scripts/eval_suite.py publish --private-dir ~/eval-private-edit
    rm -rf ~/eval-private-edit

`publish` uploads the next version, checks that a download and decrypt work, and
rewrites `evals/private.yaml`. Commit that file together with the regenerated
`data/evals/`. Never commit, upload or share the plaintext directory or the key.

Git history before Phase 50 α's move to the bundle still holds the question version 1
generators, rubrics and design note. The questions were then regenerated as version 2,
with new seeds and new planted values; bundle v1 holds version 2 and, under `legacy/`,
the version 1 malaria key needed to grade a completed run.
