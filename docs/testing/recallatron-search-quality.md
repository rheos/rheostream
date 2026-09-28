# Recallatron search-quality regressions

This suite judges relevance against independently authored questions and labelled
answers, not overlap with another search system. It uses only invented garden and
workshop facts. It exercises the real operation dispatcher, isolated PostgreSQL
workspaces, and (for semantic retrieval) the shipped local MiniLM model and pgvector.
No production queries, source exports or model service receive fixture text.

The original corpus/oracle is unchanged. A separately frozen
[ranking-holdout.json](../../tests/fixtures/recallatron/ranking-holdout.json) tests
direct answers against nearby non-answers in both creation orders, a paraphrase
and an unrelated no-answer query, at both k=1 and k=3. The original shed-key case
also runs at k=1; passing only when multiple results were requested would leave
the common first-result request broken.

Semantic-enabled curated recall now applies a bounded local cross-encoder relevance
pass after full eligibility. It uses `Xenova/ms-marco-MiniLM-L-6-v2`, pinned to
revision `a09144355adeed5f58c8ed011d209bf8ee5a1fec` (Apache-2.0), through the
already-pinned FastEmbed runtime. Both the original suite and holdout require that
real relevance model whenever there are semantic results; model failure/degradation
does not turn the quality gate green. No new dependency, embedding rebuild, source
transfer, hosted inference or history-search expansion is involved.

The corpus and oracle are in
[search-quality.json](../../tests/fixtures/recallatron/search-quality.json).
Questions are not copied record titles. Similar-topic distractors distinguish water
maintenance from electrical work, paying invoices from filing them, and the shed key
from the house key. Relevance labels identify records that actually supply the
requested information, not every record containing a query word.

## Run and read the results

Install the optional local embedding runtime and use a test-only database:

```sh
uv sync --frozen --extra local-embeddings
make test-pg-up
RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@127.0.0.1:5434/postgres \
  uv run pytest -q -s tests/test_search_quality_scores.py tests/postgres/test_search_quality.py
```

The ordinary CI pytest step also collects these tests. They explicitly select each
strategy, regardless of criterion-31 environment variables. Missing model/runtime,
wrong strategy, or a degraded dense arm fails; it never substitutes fake vectors or
silently skips the semantic pass. A cold cache needs the public model download. Once
cached, inference is local. No changes to production settings are made.

Each printed case reports:

- Recall@3: fraction of labelled answers found in the first three results.
- Precision@3: labelled answers divided by the fixed cutoff of three, even when
  fewer results are returned. A short result list must not inflate this measure.
- Reciprocal rank: one divided by the first relevant rank within the cutoff, or
  zero if none appears.
- False-positive count: unlabelled records returned within the cutoff.

The gate requires every labelled answer in the first three and a relevant record
first. The ambiguous invoice question has two relevant answers: finding only one is
not enough. The two unrelated no-answer cases require no results at all. For them,
recall is undefined (`None`), not a fabricated perfect score. The metric tests also
prove that nonempty wrong results and duplicates cannot pass as useful answers.

Exact account-code and reserved-domain contact queries test lexical and hybrid
recall. They are intentionally not claims that MiniLM understands arbitrary
identifiers. Paraphrase gates test dense and hybrid with real embeddings; lexical
search is not expected to bridge vocabulary with no shared useful terms.

History has its own lexical pass over the same labelled corpus. A separate
unconfirmed procedural note must keep its status and source context, be searchable
by the owner, be refused to a member, and not appear as a curated memory. This does
not add semantic history search or broaden the MCP surface.

## What a green run does not establish

This is a small regression corpus, not a calibrated estimate of quality on all data.
Precision@3 is reported, not claimed to be perfect: useful answers must rank first,
but related distractors can still follow them. The no-answer cases are unrelated
topics; they do not prove rejection of an unanswered question close to an existing
topic. Search retrieves records, not verified generated answers.

The test harness fills embeddings synchronously after creating the corpus, using the
real rebuild implementation. It tests steady-state retrieval, not worker lag or
just-written dense-only visibility. It does not measure large-corpus approximate
index performance, latency, or tune relevance floors, fusion weights or HNSW.

The relevance window is the first three times k admitted candidates (at most 150),
not the entire corpus. A better answer outside that first-stage window cannot be
rescued. The unchanged dense floor still admits/rejects dense candidates. The local
reranker uses its model's bounded tokenization, so long documents can be truncated;
this suite does not establish ranking quality for facts beyond that token window.
Relevance scores are raw logits, not confidence probabilities or verification of a
claim. Unanswered near-topic questions can still return related notes. A failed pass
preserves the first-stage order and reports `rerank_degraded=true`, not success.

Add new questions and labels from their intended meaning **before** inspecting a
new result ranking. When a case fails, preserve the question and labels while
investigating; do not bless whichever record happened to rank first. Any legitimate
oracle correction needs an explanation. Future private relevance review must keep
real queries, labels, source records and reports outside the public checkout and CI.
It remains separate from source preservation, erasure, producer/drain proof and the
final migration decision.
