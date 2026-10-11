# Design decisions

Judgement calls made while building this, and the reasons for them. None of them changes an invariant in SPEC.md section 5.

1. **Selecting a successor retires the predecessor.** `select_revision` retires the current selection in the same transaction by default. With `retire_current=False` it fails on the partial unique index instead. The `duplicate_case_set` fixture uses the failing path and `issuer_guidance_cut` the replacing one.

2. **Review before selection.** A revision must be `reviewed`, with a human review receipt, before it can be selected. The schema refuses `draft -> selected`.

3. **Actor kinds instead of an identity registry.** Authorities are `<kind>:<name>`. The kernel enforces rules by kind (models never review, select, approve or activate; only `direct_user` activates a mandate; executions come from a venue or system) and two separation rules by name (a reviewer is not the draft's author; an approver is not the decision's proposer). It does not authenticate names, because that depends on the application; `Store(authorize=...)` is the hook for it. Names are compared ignoring case and kind, so `direct_user:owner` and `reviewer:Owner` are one person. A solo investor who types a trade proposal themselves therefore needs a second person to approve it; the usual flow is that the agent proposes and the investor approves.

4. **Corrections and supersession respect authority.** A claim can only supersede a claim of equal or lower authority. A lower-authority source that disagrees records a contradiction. A same-tier update (new guidance replacing old) is an ordinary supersession.

5. **Contradiction status.** `contradict` marks both claims `contradicted`. A human resolution supersedes the loser and returns the winner to `active`. `unresolved` is available to callers and never set by the kernel.

6. **Freshness keys.** The policy looks up the first segment of the aspect (`price` in `price.last`) before the claim kind, so a price can have a tighter clock than a generic fact. `correction` and `user_decision` claims do not expire by default.

7. **Expiry runs from observation.** A claim's default expiry and its age in a packet are measured from its `valid_from`, else from its evidence's `observed_at`. The time a claim was typed (`created_at`) does not make old data fresh.

8. **Degraded versus expired.** Past `expires_at` is `expired`. Older than half its clock is `degraded`. Both stay in the packet with a label and a warning.

9. **Authority before similarity.** Rank is authority, then lexical overlap. On a factual query (task kind `factual_state` or `research_update`, and no sentiment words), social chatter is left out whenever primary or system evidence is present. On a sentiment query it stays, ranked after primary.

10. **Two time cuts.** `as_of` cuts by valid time. `known_as_of` also cuts by the recording times the store sets itself (`ingested_at`, `recorded_at`, relation `created_at`, receipt `occurred_at`, which callers cannot set and the schema keeps in order) and rebuilds claim and revision statuses from those times. Retractions write a receipt so that they can be dated. Question, invalidation and trigger states are not rebuilt.

11. **What the packet hash covers.** Both time cuts, policy version, task, query hash, selected refs with their object hashes, omissions, sorted warnings and the runtime block. Not the packet id or `created_at`, so a rebuild against the same data gives the same hash under a new id. Changing the model changes the hash.

12. **Contradiction parties keep their own rank.** Both sides of an open contradiction, and the selected revision, are kept ahead of the budget. Their authority rank is not changed; an earlier version raised it, which let a weak claim borrow authority.

13. **Mark staleness.** A mark may lag its position by `freshness_seconds["mark"]` (default one hour) before a warning, and always warns when its source is `stale_cache`. So a Friday equity close next to a Sunday crypto position warns, and a live crypto mark one minute older than its snapshot does not.

14. **Choosing a position.** Among position-grade snapshots at or before `now`, fresh ones win over stale ones, then the higher source class, then the newer. A newer position-grade snapshot that reports a different quantity is warned, so a stale reconciled holding cannot silently hide a fresh broker position.

15. **Who checks the mandate.** The kernel computes the check inside `propose_decision`; the caller cannot pass one. NAV and current exposure still come from the caller, because the kernel does not value portfolios. The check records them and who supplied them, lists the limits it did not check, and treats a missing NAV on a buy as a breach rather than a pass.

16. **Daily notional.** The daily cap counts decisions in the same scope approved on the same UTC date that are still approved or executed. The check runs at proposal and again at approval inside the write lock, so several proposals that each passed cannot all be approved past the cap.

17. **Approvals lapse.** An approval older than the mandate's `approval_ttl_seconds` (default one day) cannot be executed. `expire_decision` records that it lapsed.

18. **Excess slippage is flagged in the receipt.** An execution receipt stores the expected price, the fill, slippage in basis points and whether the fill was partial. The sign follows the side of the trade: buys and covers pay more when the fill is above expected, sells and shorts lose when it is below. Positive is a cost. A `reduce` or `close` needs an explicit `side` to be measured. A fill beyond `max_slippage_bps` is flagged in the receipt. It is not refused, because the fill has already happened.

19. **Rejections, expiries and outcomes are review receipts.** There is no separate receipt kind for them. The payload says which. An outcome does not change the decision row.

20. **One receipt chain per store.** Every receipt links to the previous one, whatever object it is about. The schema refuses a receipt that does not link to the current head, so concurrent writers cannot fork the chain; the second waits for the write lock and links to the first. Hashing happens in Python, so the schema cannot check that a chain hash is correct, only that it links. `verify_chain` checks correctness.

21. **Fixture format.** One JSON operation per line, replayed through the public functions. `as` names a result, `$name` refers to it, `expect_error: true` marks an operation the kernel must refuse, and `expect` lines are assertions checked at that point. Fixed clock and counter ids make replays identical.

22. **Health checks.** Fixture results can be stored as `health_check` receipts with the fixture's sha256 and pass or fail. Nothing here schedules them.

23. **Sells must cite a holding.** Direction is declared by the caller, and decreasing trades skip the position limits. To stop a "sell" from opening a short that no limit sees, a sell, reduce or close must cite a position-grade snapshot with a positive quantity and cannot exceed its marked value, and a cover must cite a short.

24. **Reaffirming re-points the dependency.** When a human reaffirms a stale claim, it gets a `depends_on` edge to the current successor of each basis that changed. The old edges stay for history; changes older than the reaffirmation no longer warn.

25. **Receipts vouch for rows.** Approval and execution receipts store the proposal's hash, activation the mandate body's, selection the revision's assumptions and evidence hashes, reconcile the snapshot's. `verify_chain` checks them, so a proposal edited after approval is detected even if the triggers were dropped.
