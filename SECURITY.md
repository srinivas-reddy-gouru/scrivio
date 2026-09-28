# Security

## What this is built for

One person, running it on their own machine. It listens on loopback, asks a
browser to pair before showing anything, and keeps resumes, interview answers,
and keys in files on that machine.

It is **not** built to be hosted for several people. There are no user
accounts and no separation between one person's records and another's: anyone
who pairs sees everything. Reports about that are welcome, but it is a known
limit and not a vulnerability. `docs/OPERATIONS.md` lists what hosting would
need.

## Reporting a problem

Please do not open a public issue for something that could expose a person's
resume, answers, or keys.

Use GitHub's private vulnerability reporting: the **Security** tab of the
repository, then **Report a vulnerability**.

> Owner: this has to be switched on in the repository settings (Settings, then
> Code security, then Private vulnerability reporting). Until it is, this
> section points at a button that is not there. Remove this note once it is
> on, or replace the paragraph above with the contact you prefer.

Include what you did, what happened, and what you expected. A request that
reproduces it against a demo-mode server is the most useful thing you can
send. Do not send real resumes or keys.

This is maintained by one person in their own time. There is no promised
response time and no bounty.

## What is in scope

- Reading or changing stored data without a paired session.
- Getting the server to fetch an address inside the network it runs on.
- Script running in the article reader or either interface.
- A path that writes model output to a resume without the fact guard.
- Downloading a tailored resume as finished while it holds a placeholder.
- A key appearing in a response, a log, an export, or an error.
- Anything that lets a web page the user visits act on the local server.

## What is already known

- One person per install, as above.
- Plain HTTP. Acceptable on loopback, and not anywhere else.
- Resume text and answers are sent to the model provider you configure. That
  is how it works, and the interface says so before anything is sent.
- The injection filter reads the start of each statement. It is one layer:
  external text is also fenced and labelled as data in the prompt.
- The older interface at `/classic` builds pages from strings. Its rendering
  of generated content goes through the same sanitiser, but it has had less
  review than the current interface.
