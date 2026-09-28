# Curated recall: post-eligibility relevance ranking

PR #203 / issue #204 amend the semantic-enabled ordering described in
[storage and workspaces](storage-and-workspaces.md). First-stage lexical, dense and
RRF scores, dense floors, stored vector representations and the lexical-only
history archive are unchanged. The original RRF recency tie-break remains the
first-stage fallback, not the final relevance rule on a healthy local semantic read.

For curated `dense` or `hybrid` recall using the local embedding provider, walk the
first three times requested k readable candidates, capped at 50, using the existing
500-candidate and shared 4096-reference/depth-64 bounds. Full eligibility, including
permission-bearing sources, precedes any text reaching the local relevance model.
Budget exhaustion refuses the whole read before that call, without partial content.
Hybrid proves fusion's admitted top-window instead of only top-k; its existing dense
arm over-fetch remains based on the original requested k, not on the larger window.
This costs additional bounded eligibility work and can refuse a request whose
previous smaller walk fitted the reference budget. It never grants wider access.

Rank this window by the pinned local MS MARCO MiniLM cross-encoder's query/document
relevance logit, descending. Stable equal scores preserve the first-stage ordering.
Return at most k. There is no new score floor, identifier redaction, title-special
case, negation heuristic or query-specific exception. Model inference is on-box;
only the immutable public Apache-2.0 artifact is fetched into core's model cache.
The optional existing local-embeddings extra supplies the runtime. Loading is lazy
and process-resident, including in each serving process. The artifact and bounded
tokenization impose a cold-load cost and a long-document truncation limitation.

`score` and `strategy` retain their first-stage meanings. When applied, provenance
names `reranker` as model plus immutable revision, and items carry `rerank_score`.
After reranking, base scores need not descend. Raw rerank scores are not confidence
probabilities and must not be compared across queries. A model/runtime failure
preserves the original order, sets `rerank_degraded=true`, leaves `reranker` and
`rerank_score` null, and logs only the exception class, not query/document/error text.
A failed cold load is shared across concurrent callers and backed off for five
minutes measured from failure completion; later calls degrade immediately instead
of repeating a network timeout. A subsequent retry can recover without a restart.
An empty or unavailable-semantic answer does not invoke the model. Lexical-only
and non-local providers retain their original walk and ordering.

The frozen original quality oracle and independently frozen holdout test relevance
directly, not agreement with the predecessor. Related false positives can remain;
the pass does not find answers outside the first-stage candidate window or establish
that any claim is current/true. Owner-only history search remains lexical and
status-bearing. No migration, writer activation, release or cutover is authorized
by this ordering amendment alone.
