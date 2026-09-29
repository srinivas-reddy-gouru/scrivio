# Prompt templates

Every prompt sent to a model as a system prompt is a file in
`pipeline/prompts/`. `tests/test_prompt_files.py` reads the source and fails
if one is written inline, if the code names a file that is not there, or if a
file is there that nothing uses.

Files are loaded through `pipeline.prompt_loader.load_prompt`, which expands
`{{include:_fragment_v1.txt}}` one level deep. Files beginning with an
underscore are shared fragments.

## Names that do not follow the rule

The project rule is that every template carries a `_v1` suffix. Eleven do not:

| File | Loaded by |
| --- | --- |
| `brief_v2.txt` | `pipeline/workers/brief_worker.py` |
| `compiler_v2.txt` | `pipeline/workers/compiler_worker.py` |
| `critic_v2.txt` | `pipeline/workers/critic_worker.py` |
| `diagram_spec_v2.txt` | `render/mermaid_worker.py` |
| `drafter_v2.txt` | `pipeline/workers/drafting_worker.py` |
| `editor_v2.txt` | `pipeline/workers/editor_worker.py` |
| `planner_v2.txt` | `pipeline/workers/planning_worker.py` |
| `polisher_v2.txt` | `pipeline/workers/humanization_worker.py` |
| `relevance_checker_v2.txt` | `pipeline/workers/relevance_worker.py` |
| `verifier_v2.txt` | `pipeline/workers/verification_worker.py` |
| `vhs_tape_v2.txt` | `render/mermaid_worker.py` |

All eleven belong to the article pipeline and date from the July 2026 rewrite,
when each replaced a `_v1` file of the same name. They are second versions and
are named as such.

They have been left as they are, deliberately. Renaming them to `_v1` would
make the name say something untrue, and would collide with the history of the
files they replaced. The two honest ways to settle it are:

1. **Change the rule** to "a `_vN` suffix, and a new file for each new
   version". This matches what the repository already does and costs nothing.
2. **Rename the files** to `_v1` and treat the suffix as a format marker
   rather than a version. This needs the references in the table updated and
   `_CACHE_VERSION` in `pipeline/cache.py` raised.

Which one is the owner's decision. Until it is made the list above is closed:
the test fails if a twelfth name joins it.

## Moved out of the code in this review

These were string literals. What is sent to the model is unchanged, character
for character, which was checked by comparing the loaded text with the literal
it replaced before the literal was removed.

| File | Was in |
| --- | --- |
| `resume_summary_condenser_v1.txt` | `pipeline/workers/resume_studio_worker.py` |
| `official_sources_v1.txt` | `pipeline/workers/source_resolver.py` |
| `topic_classifier_v1.txt` | `pipeline/workers/topic_classifier.py` |
| `search_queries_v1.txt` | `main.py` |
| `workflow_search_queries_v1.txt` | `pipeline/orchestrator/article_workflow.py` |
| `claim_search_query_v1.txt` | `pipeline/workers/verification_worker.py` |

`search_queries_v1.txt` and `workflow_search_queries_v1.txt` say nearly the
same thing. They were two different prompts before and are two files now,
because merging them would change what one of the two paths sends.

## What the test does not see

It finds a system prompt passed as `system=` or as a `{"role": "system"}`
message. It does not find instructions assembled into a **user** message, and
several workers do build those in code around the data they send. Those are
not templates in the sense of the rule, but the line between the two is a
judgment and the test does not make it.
