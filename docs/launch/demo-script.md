# Demonstration, three minutes

Runs in demo mode, so it needs no key, no resume, and no network, and it
cannot spend anything. The person and the posting are invented.

Everything on screen in demo mode is a fixed example and the banner at the top
says so. **Say that out loud at the start.** The demonstration shows how the
workflow goes. It does not show what a model would write, and the recording
must not be captioned or described as if it did.

Each step below is run by a browser test, named beside it. If a step here
stops working, that test fails.

## Before

```bash
(cd web && npm ci && npm run build)
SCRIVIO_DEMO=1 ARTICLE_OUTPUT_DIR=/tmp/scrivio-demo python -m api
```

Use an empty output folder, so the home page opens as a new install does. Open
http://localhost:8899 and pair with the code from the terminal.

## Steps

| | Do | Say | Test |
| --- | --- | --- | --- |
| 1 | Show the home page. Point at the banner. | "This is demo mode. What you will see are fixed examples, and it says so on every page." | `test_a_first_visit_says_who_it_is_for_and_offers_a_sample` |
| 2 | Choose **Try it with a sample resume**. | "This person is invented. You would paste your own resume and a posting." | `test_the_sample_runs_end_to_end_and_is_labelled_throughout` |
| 3 | Choose **Read my resume**. Show the score and open two rows. | "The score is a checklist. Each row says why it passed or failed. It is not a prediction of what a tracking system will do." | same |
| 4 | Choose **Tailor it to this JD**. Point at the amber `[METRIC]`. | "The rewrite wanted a number here that the resume does not have. It was not made up. It is left for you." | `test_resume_import_review_edit_and_export` |
| 5 | Choose **Send**. Show that it refuses. | "It will not give you a finished copy with a blank in it." | same |
| 6 | Go back, click the chip, type `35`, save. Choose **Send**, then **Markdown**. | "Now it is your number, and the download opens." | same |
| 7 | Open **Interviews**. Topic: `Kafka consumer groups`. **Take a seat**. | "The rubric was written before I answer, and I cannot see it." | `test_a_mock_interview_from_start_to_feedback` |
| 8 | Type two sentences. **Submit answer**. Show the feedback. | "In demo mode this feedback is an example. With a provider it is graded against that rubric." | same |

## If asked

**"Does it work on a real resume?"** Yes, with a provider configured. This
demonstration does not show that, and you should not claim it does from this
recording.

**"How good is the tailoring?"** Not measured. The fact checks are tested.
The quality of the prose has not been evaluated.

**"Is the grading accurate?"** Not measured. See `evals/README.md`.

## After

Delete `/tmp/scrivio-demo`.
