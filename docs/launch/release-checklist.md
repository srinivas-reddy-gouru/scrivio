# Before tagging a first release

Nothing here has been done. Tick a line when it has been done and checked,
not when it has been planned.

## The code

- [ ] The review branch is merged to `main`.
- [ ] `python -m pytest tests/ -q` passes on a clean checkout, including the
      browser and restart tests (not skipped).
- [ ] `python -m evals.resume_guard_eval` exits 0.
- [ ] `(cd web && npx tsc --noEmit -p tsconfig.json && npm run build)` passes.
- [ ] `sh scripts/smoke_install.sh` passes from a clean clone.
- [ ] CI has run on GitHub and every job is green. It has been written and
      has never run.
- [ ] `python -m api.doctor` reports no failures on the machine used for the
      release.

## A real provider, once

The suite runs on canned clients. Before a release, one person runs each of
these against a real provider, with the **sample** resume, and writes down
what happened:

- [ ] Read and tailor the sample resume. The result has no invented figure.
- [ ] One practice interview question, answered and graded.
- [ ] One job target created from the sample.
- [ ] Record the provider, the model, and the date.

This costs a small amount. It is the only evidence that the release works
with a real model, so it is not optional and a canned run does not replace it.

## The repository

- [ ] No personal data in the tree: `git grep` for the owner's name, email,
      phone, and employers returns nothing outside `LICENSE` and commit
      metadata.
- [ ] README screenshots were taken by `scripts/capture_screenshots.py`.
- [ ] Decide what to do about earlier screenshots in the history of `main`,
      which show the owner's name and a target employer. Replacing the files
      does not remove them from history. Rewriting history is the only thing
      that does, and it breaks existing clones.
- [ ] `.env` is not tracked, and `chmod 600 .env` has been run locally.
- [ ] Private vulnerability reporting is on, and the owner's note is removed
      from `SECURITY.md`.
- [ ] Description, topics, and homepage are set.

## The release

- [ ] Choose the version. `0.1.0` says what this is: usable and not stable.
- [ ] Move the "Unreleased" section of `CHANGELOG.md` under that version and
      date.
- [ ] Tag, and publish the release with the changelog section as its notes.
- [ ] Install from the tag on a machine that has never had it, following the
      README only.

## Not before

Do not announce until the last line above has been done by someone other than
the author.
