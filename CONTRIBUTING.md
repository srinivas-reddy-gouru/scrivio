# Contributing

## Set up

You need Python 3.12 or 3.13 and Node.js 20 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -c constraints.txt
(cd web && npm ci && npm run build)
python -m api.doctor
```

## Run the tests

```bash
python -m pytest tests/ -q
python -m evals.resume_guard_eval
(cd web && npx tsc --noEmit -p tsconfig.json)
```

The suite needs no keys, no network, and no provider. It cannot read or write
your own resumes, settings, or output folder: `tests/conftest.py` gives every
test its own, removes provider keys from the environment, and blocks
connections that leave the machine.

The browser tests need `web/dist` built and Chrome or Chromium. Without them
they skip, and a skipped test has not passed.

## Run it while you work

```bash
SCRIVIO_DEMO=1 python -m api       # the API, on canned examples
(cd web && npm run dev)            # the interface with hot reload, on :5180
```

Demo mode needs no provider and costs nothing.

## What a change has to do

**A rule that matters is enforced in code and covered by a test.** Asking a
model not to invent a number has been measured failing here. If a change
depends on a model behaving, add the check that catches it when it does not.

**Reproduce the defect before fixing it.** Write the test, watch it fail for
the reason you expect, then fix. A test written after the fix proves only that
the test passes.

**Invented data only.** No real resume, name, employer, or posting in a test,
a fixture, a screenshot, or an issue. Use `@example.com` addresses and
`555 01xx` phone numbers. `python scripts/capture_screenshots.py` takes the
README pictures from a demo server of its own and cannot be pointed at yours.

**Say only what is tested.** If the README or the interface makes a claim,
there is a test behind it, and the claim goes no further than the test does.

**No model calls in tests.** Anything that needs a real model goes under
`evals/` and runs only when asked.

## House rules

- Contracts between stages are Pydantic v2 models, not raw dictionaries.
- Prompts are files in `pipeline/prompts/`. See `docs/PROMPTS.md`.
- Keys come from the environment. None in code, none in tests, none in logs.
- Text fetched from the web goes through `injection_filter()` before any
  model call.
- No em or en dashes in anything the product writes or shows.
- Ask before adding a production dependency, changing a schema that has
  tests, or changing `injection_filter()`.

## Pull requests

Say what was wrong, how you reproduced it, what you changed, and which tests
you ran. If something is not covered, say that too.

## Security problems

Not in a public issue. See [SECURITY.md](SECURITY.md).
