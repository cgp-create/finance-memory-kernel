# Implementation notes

Ambiguities in the two source specifications and how this implementation resolved them. Each is a judgement call; none changes an invariant.

1. **Selection replacing a selected revision.** The canonical spec says at most one selected revision per subject and horizon, but not whether selecting a successor retires the predecessor or fails. `select_revision` retires the current one by default (`retire_current=True`) inside the same transaction; with `retire_current=False` it fails on the partial unique index. The duplicate_case_set fixture exercises the failing path, the issuer_guidance_cut fixture the replacing path.

2. **Review before selection.** The canonical spec's invariant 5 says the selection receipt names "the reviewed revision". This is read as a requirement: a revision must be in status `reviewed` (with a review receipt) before it can be selected. A draft cannot be selected directly.

3. **Who may select.** The spec says "human or authorized review". `authority_ref` strings beginning with `model`, `retrieval` or `assistant` are refused for selection and for contradiction resolution. Everything else is accepted; the kernel does not maintain an authority registry.

4. **Correction authority.** The canonical test 1 says a "direct user or primary correction replaces an older inference". `claims.correct` requires the correcting evidence's authority tier to be at least that of the old claim's `source_strength`; a lower tier must use `contradict` instead. Plain `supersede` has no authority check, because a same-tier update (new guidance replacing old guidance) is ordinary.

5. **Contradiction status.** The enum has both `contradicted` and `unresolved`. `contradict` sets both claims to `contradicted`; `unresolved` is left available for callers but is not set by the kernel. An authorized resolution supersedes the loser and returns the winner to `active`.

6. **Freshness policy keys.** The spec says "by claim kind" but the examples (price, holdings, estimates, calendar) are aspects. The policy looks up the first segment of the aspect (`price` in `price.last`) before falling back to the claim kind. `correction` and `user_decision` never expire by default.

7. **Degraded versus expired.** Invariant 10 says facts "expire or degrade". A claim past `expires_at` is `expired` and warned; a claim older than half its clock is `degraded` and warned; both stay in the packet with a freshness label. Nothing is silently dropped.

8. **Authority before similarity.** The spec says not to promote low-authority commentary over primary evidence "because it is semantically closer". Rank order is authority first, then lexical overlap, always. On a factual query (`task_kind` in `factual_state` or `research_update`, and no sentiment words in the query), social chatter is additionally omitted when primary or system evidence is present. On a sentiment query it stays, ranked after primary.

9. **Future leakage cutoff.** `as_of` cuts evidence by `observed_at` and claims, questions, invalidations and revisions by `created_at`. Later objects are not listed as omitted either; from the packet's point of view they do not exist. Triggers have no creation time in the spec and are included whenever active.

10. **Packet hash coverage.** The hash covers `as_of`, policy version, task, query hash, selected refs with their per-object hashes and as_of, omitted, sorted warnings and the runtime block. It does not cover the packet id or `created_at`, so a rebuild against the same data yields the same hash with a new id. Changing the model id changes the hash, since the runtime is part of what was supplied.

11. **Runtime block fields.** The extension lists `model_id, model_version, provider, route_id, policy_version, tool_versions, source_feed_as_of, fallback_used, fallback_reason`. The first four and a boolean `fallback_used` are required; `policy_version` defaults to the policy in use; `tool_versions` and `source_feed_as_of` default to empty. The schema CHECK requires `model_id`, `policy_version` and a boolean `fallback_used`.

12. **Mark staleness.** Extension test 17 asks for a warning when `mark_as_of` is stale relative to `as_of`. The reference extension warned whenever the mark was older than the position by any amount, which would warn on a live crypto mark one minute older than its snapshot. This implementation allows a lag of `freshness_seconds["mark"]` (default 3600 seconds) before warning, and always warns when `mark_source_class` is `stale_cache`. The weekend_mixed_marks fixture therefore warns on the equity and not on the crypto asset.

13. **Snapshot age and withheld quantities.** A position-grade snapshot past its freshness limit is served with its quantity and a warning. Quantity is withheld only when the best snapshot is not position-grade (`plan`, `estimate`, `stale_cache`). "Best" is highest source class, ties broken by newest `as_of`.

14. **Reconcile binds a reconciled snapshot.** The extension says reconcile binds the fill "to a new PositionSnapshot". This implementation requires that snapshot to have `source_class='reconciled'`; binding a plan or an estimate is refused.

15. **Rejection and outcomes as review receipts.** The extension has no receipt kind for rejection or outcome. Both are written as `review` receipts bound to the decision, with a payload saying which. The decision row does not change when an outcome is attached; what happened afterwards is evidence about the decision, not a status of it.

16. **Mandate retirement.** The extension does not say who may retire a mandate. `retire_mandate` writes a `review` receipt with the given authority (default `direct_user:owner`) and requires the mandate to be `active`.

17. **Decision subject foreign key.** `decision_record.subject_id` references `subject`, so the ported extension tests create subjects `SUBJ_A`, `SUBJ_0..2`, `EQ_1`, `EQ_2` in a fixture before use. The reference extension had no subject table.

18. **Fixture format.** The canonical spec names `.jsonl` fixtures but gives no format. Each line is one operation (`op`) replayed through the public functions, with `as` to alias the result, `$alias` references, `expect_error: true` for operations the kernel must refuse, and `expect` lines for assertions. Replays use a fixed clock and counter-based ids so two loads of one fixture are identical, which is what the replay test depends on.

19. **Fixture health checks.** Known-answer fixtures are stored as `health_check` receipts keyed by fixture name with the file's sha256 and pass or fail. The test suite writes one per fixture; nothing schedules them.

20. **Benchmarks removed.** The canonical spec's section 12 and implementation steps 6 and 7 described an experiment design against a public continual-learning benchmark. They are replaced by SPEC.md section 15, which states that no evaluation was run and that no behavioural or outcome claim is made. The `benchmarks/` folder is omitted.

21. **Production details in section 9.** Counts and limitations of the production system were removed; section 9 now only states the relationship.

22. **Em dashes.** None appear in prose files. Lists in running text use commas or semicolons.
