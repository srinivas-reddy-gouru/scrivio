# Article Grading Rubric

Score each axis 1–5. A 5 means "no editor would touch it"; a 3 means
"publishable after minor edits"; a 1 means "would embarrass the blog".
Judge the article as a reader who found it via search, not as its author.

## 1. factual_accuracy
Are the technical claims correct and current? Version numbers, defaults,
API names, and mechanism descriptions must match reality. Penalize confident
claims that are wrong harder than hedged uncertainty.

## 2. code_correctness
Would the code blocks compile/run as written? Consistent indentation (2/4
spaces, never 1), correct language for the topic, complete enough to use,
no pseudocode twins next to real code.

## 3. diagram_clarity
Does each diagram communicate ONE thing accurately? Arrows match the prose's
direction of flow, failure paths are visually separate from solution paths,
retry loops point at the step actually retried, ≤ ~12 nodes.

## 4. citation_quality
Are factual claims cited, and are the sources worth citing? Official docs and
engineering blogs beat social posts. Citation coverage should not collapse in
later sections. A Sources list of one or two URLs for a long article is weak.

## 5. prose_naturalness
Does it read like a colleague wrote it? Specifically penalize: comma splices,
stock phrases ("the structural fix is", "worth naming", "it's worth noting",
"delve", "robust"), every section following the identical internal template,
self-referential labels ("this article covers"), and zinger-ending paragraphs.

## 6. overall_publishable
"Would I publish this under my own name?" Gestalt judgment: does the article
argue a thesis, serve the promised reader, and end somewhere actionable?
