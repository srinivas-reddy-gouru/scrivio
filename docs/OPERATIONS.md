# Operating Scrivio

This covers running Scrivio for **one person on their own machine**, which is
what it is built for today. The last section lists what hosting it for several
people would additionally need. None of that exists yet, and nothing here
should be read as saying it does.

## Is it working?

| Question | Ask | Answer |
| --- | --- | --- |
| Is the process up? | `GET /health` | Always `{"ok": true}` while it is running. No session needed. |
| Can it do real work? | `GET /ready` | `200` when it can, `503` when it cannot, with each check named. No session needed. |
| What is it doing? | `GET /diagnostics` | What is running, what has gone quiet, how long stages took. Needs a paired session. |
| Is the install sound? | `python -m api.doctor` | Runtimes, packages, the interface build, provider, storage, file permissions. |

None of these calls a model. A check that did would be a charge on every poll.

`/ready` reports four checks:

- `storage`: the output folder can be written to.
- `provider`: `ok`, `demo`, `missing`, or `signed out`.
- `interface`: `ok` or `not built`.
- `capacity`: `ok` or `full`. Full does not make it unready: it will answer
  `429` to new article runs until one finishes.

## What is limited, and what is not

All of these are held by one process. With two worker processes each limit
doubles, and none of them knows who is asking.

| Limit | Value | What it covers |
| --- | --- | --- |
| Article runs at once | 2 | A run, from start to finish |
| Resume analyses and tailoring at once | 3 | The work that goes on after the request returns |
| Requests that reach a provider, at once | 4 | Every request that calls a model, fetches a posting, or sends audio, for as long as it is being answered. Includes the topic classifier behind `POST /generate` |
| One provider call | 300 seconds | From when it is made to when it returns, counting every retry |
| One wait inside a provider call | 180 seconds, 10 to connect | To connect, to send, and between one piece of a response and the next |
| Retries of a provider call | 2 | The SDK's own. They are inside the 300 seconds, not added to it |
| One run of a command-line assistant | 180 seconds | One call can be two runs. The call has 300 seconds |
| Fetching one page | 25 seconds, 3 MB | After decompression |

Past a limit the answer is `429` with `Retry-After`. Nothing is queued.
`/diagnostics` shows how many of each are running.

**Not limited:** how much is spent. There is no budget, and no count of
tokens or of calls over time. Once an article run has been admitted, the calls
it makes are bounded by the pipeline and by the limits on a single call, not
by the limit of four. A person who starts article after article, two at a
time, all day, is not stopped.

None of these limits was measured against a real provider.

Only `storage` and `provider` decide the status code.

## Reading the log

Started with `python -m api`, each line carries the request and job it
belongs to:

```
2026-09-28 12:00:01 ERROR [req 9f2c41d07a3b55e1 job 3fa85f64] root: Job 3fa85f64 failed: ValueError
```

Every response carries its request id in `X-Request-ID`. When a run fails, the
message shown to the user ends with a reference, for example
`Reference: 3fa85f64 (ValueError)`. Search the log for that reference to find
the detail.

What is deliberately **not** in the log: request bodies, resume text,
interview answers, keys, and tracebacks from code that handles resumes or
answers (a validation error quotes the input it rejected).

## When something has stalled

`/diagnostics` lists a run under `stalled` when it is still running and has
reported nothing for ten minutes. Scrivio does not stop it for you: it may be
waiting on a slow provider, and the call is yours. To stop it, use **Stop this
run** on the progress screen, or:

```bash
curl -X DELETE http://localhost:8899/jobs/<job id> -b "scrivio_session=<your session>"
```

A provider call ends within 300 seconds, counting retries. A run of a
command-line assistant is stopped after 180 seconds, together with whatever
it started. See "What is limited, and what is not" above for exactly what
each of those means.

## Stopping and starting

Stop with Ctrl-C or `SIGTERM`. On a clean stop, work in flight is marked
**interrupted** before the process exits.

If the process is killed or the machine loses power, nothing is marked at the
time. The next start finds what was left as "running" and marks it
interrupted then.

Either way, **nothing is restarted automatically**. Every article run,
analysis, and tailoring pass is a series of paid model calls, and starting
them again without being asked would spend your money to recover from our
crash. The interface shows what was interrupted and offers the retry.

What survives a stop: every saved resume, job target, interview, and article;
the record of each article run, including the progress it reported; settings;
and paired browsers.

What does not: a run in progress. Its stages are cached, so starting it again
repeats only the stages that had not finished.

## Backup, restore, and going back a version

```bash
python -m api.data backup  before-upgrade.zip
python -m api.data restore before-upgrade.zip --replace
```

Before upgrading, take a backup. To go back:

1. Stop the server.
2. Check out the previous version and reinstall:
   `pip install -r requirements.txt -c constraints.txt`, then
   `(cd web && npm ci && npm run build)`.
3. `python -m api.data restore before-upgrade.zip --replace`. What was there
   is saved first as `_before-restore-<time>.zip`.
4. `python -m api.doctor`, then start the server.

Saved records carry no version number because they have not needed one: every
field added so far has a default, so older records load as they are. A change
that breaks that needs a migration and a test of it, and must not be released
without both.

A damaged record is never deleted. It is renamed with `.corrupt` and reported,
and `python -m api.data show` counts them under `set_aside`.

## If something private may have been exposed

1. Stop the server.
2. **Sign out every browser.** Delete `.scrivio/session.key`, or once it is
   running again call `POST /auth/forget-all`. Every session stops working.
3. **Replace any key that may have been seen**, at the provider. Replacing it
   in Settings changes which key Scrivio uses; it does not disable the old one.
4. Check `/diagnostics` and the log for requests you did not make. Each has a
   request id.
5. If resumes may have been read, remember they also went to your provider
   when they were analysed. Deleting them here does not recall them.

## What hosting for several people would need

Scrivio is not ready to be hosted for more than one person, and the gap is not
small. Each line below is something that does not exist today.

| Need | Today |
| --- | --- |
| Users, and a check on every record that it belongs to the one asking | One session secret. Everyone who pairs sees everything. |
| Credentials per user | One settings file and one process environment. |
| Work that survives a restart | Runs happen inside the web process and are lost with it. |
| Limits shared between worker processes | Counters in one process's memory. With two workers the limit doubles. |
| Interrupted-work detection that is correct with several workers | Each worker would mark the others' running jobs as interrupted. |
| Encryption in transit | Plain HTTP, acceptable on loopback only. |
| Restrictions on outbound traffic at the network | Enforced in application code only. |
| Alerts | None. `/ready` and `/diagnostics` are there to be polled by whatever you use. |

Sensible first alerts, if you wire them up: `/ready` failing for more than two
minutes; any run in `stalled`; `storage` failing at all; `set_aside` greater
than zero.
