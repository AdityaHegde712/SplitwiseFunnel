# SplitwiseFunnel Handoff: Owner-Linked Refund Adjustments

## Mission

Continue on `dev` only. Implement refund handling for Instacart Walmart receipt emails **backend first**. The owner has explicitly decided that a linked refund is divided equally among everyone present on the original selected receipt's purchase date. Do not build or change the refund UI until request and response schemas are tested and fixed.

## Repository Rules

- Never inspect, edit, or search `.env` files. Preserve user-owned changes: `.gitignore`, `.agent-tasks/`, and `start.sh` were uncommitted at handoff.
- Work only on `dev`; never merge or push `main`. Verify branch/status before each conventional commit. Do not weaken, delete, or modify existing test assertions.
- Use `apply_patch` for edits. Keep UTF-8/LF. Update `.agent-tasks/PLAN.md`, `DECISIONS.md`, and `TASKS.md` after each meaningful boundary; they are intentionally ignored.
- Use `tester`, `backend-engineering`, and `clean-code` for backend work. Use `frontend-engineering` and `webapp-testing` only after the backend contract is frozen.
- Gmail data is historical. Cache exact selections and process locally. Do not repeatedly query Gmail; never cache partial/429 responses. Do not log tokens, raw email bodies, MIME, item names, card data, or attachments.

## Locked Product Decisions

1. Refunds are not silently skipped or treated as a generic unsupported format.
2. MVP only covers Instacart Walmart refund emails. Costco refund support is deferred.
3. The owner chooses which parsed original Instacart receipt receives a refund.
4. The refund is a negative receipt-level adjustment. It is split equally at the cent level among participants **present/not absent** on the original receipt's purchase date. Item-level allocation rules do not apply.
5. Preserve the original receipt's payer and source amount unchanged. The negative adjustment lowers participant shares and run aggregate; it does not create a settlement algorithm or invented payment.

## Current State and Evidence

- Branch: `dev`, at handoff `ahead 2` of `origin/dev`. Latest commits:
  - `289562b feat: add aggregate receipt run totals`
  - `c32525e fix: report Gmail authorization failures`
- Last full green test run: `\.venv\Scripts\python.exe -m unittest discover -s tests -v` => 58 passed.
- `ruff` is not installed; do not alter dependencies merely to lint.
- Schema discovery report: `data/schema_discovery/instacart_walmart/processed/schema_summary.json` reports 233 messages, 73 observed variants, 59 parsed, and 14 rejected. Every rejected parsed candidate had `signals.refund_evidence: true` and `failure_code: missing_finalized_items`.
- Existing review-required cache records only retain safe metadata. They cannot yield refund amount because raw email body is intentionally not cached. To parse current historical refunds, obtain a controlled one-time exact-message read only when necessary (and only after considering Gmail rate limits/auth). Parse in memory to safe data; do not save raw body.
- Gmail has hit `Total Query Cost` per-user quota before. Exact ingestion is cache-first. Existing saved OAuth refresh token previously failed; HTTP 401 is correctly surfaced as `gmail_authorization_failed` with a renewal instruction.

## Relevant Files

- `src/app/workflow.py`: cache ingestion and `process_cached_receipts`; it currently emits parsed or `review_required/unsupported_receipt_format` records and processes cached records.
- `src/parsers/receipts.py`: finalized Instacart Walmart parser requires line items and intentionally rejects refunds. Add a separate evidence-gated refund parser; do not relax finalized-receipt parsing.
- `src/domain/allocate.py`: `allocate_receipt` derives `present_participants` from purchase-date absences, applies item rules, and uses `_split_cents` (safe for negative amounts). Expose a deliberate public helper for receipt-level eligible/present participants rather than duplicating private absence logic.
- `src/app/results.py`: `build_result`, aggregate and Markdown functions. Current receipt results reconcile allocated shares to `receipt.final_total_cents`; adapt carefully so original evidence total remains intact while net adjusted total reconciles and aggregates correctly.
- `src/app/receipt_cache.py`: exact-selection cache V1; no raw body stored. New record fields may be backwards compatible; avoid forced cache invalidation.
- `src/web/app.py`: `POST /api/v1/runs` orchestrates cache/Gmail/process/result writes and maps errors. It currently supports only `UnknownPaymentMappingError` and `ReceiptReviewRequiredError` special responses.
- `src/web/static/app.js`, `index.html`, `styles.css`: current three-tab UI. Defer changes until backend schema exists.
- Tests: `tests/spec/test_workflow.py`, `test_receipt_parsers.py`, `test_config_and_results.py`, `test_web_api.py`, `test_receipt_cache.py`.

## Recommended Backend Contract (Implement Test-First)

### Safe parsed refund record

Only after verifying an actual refund email layout from controlled/sample evidence, produce a record such as:

```json
{
  "status": "refund_pending_link",
  "refund": {
    "refund_id": "source-message-id",
    "retailer": "walmart_online",
    "refund_date": "YYYY-MM-DD",
    "refund_amount_cents": 1234,
    "source": {
      "message_id": "...",
      "vendor_id": "walmart_online",
      "email_sender": "orders@instacart.com",
      "email_subject": "..."
    }
  }
}
```

The amount must be exact and explicitly evidenced. If amount/date cannot be safely parsed, retain the present `review_required/unsupported_receipt_format` behavior. Do not claim a refund from loose wording alone.

### Pending response

When a cached run encounters an unlinked refund, fail closed with a distinct, actionable response (probably HTTP 409), for example:

```json
{
  "detail": "A refund needs an original receipt selection before this run can finish.",
  "reason_code": "refund_receipt_link_required",
  "refund": { "refund_id": "...", "retailer": "walmart_online", "refund_date": "...", "refund_amount_cents": 1234 },
  "candidates": [
    { "receipt_id": "...", "purchase_date": "...", "final_total_cents": 4500 }
  ],
  "receipt_source": "cache",
  "correlation_id": "..."
}
```

Candidates must be parsed Instacart/Walmart original receipts from the same exact cache selection. If a date comparison is possible, only offer plausible prior receipts; otherwise be conservative and make no inferred match. Do not use payment/card fields as a linkage heuristic.

### Resolution request and persistence

Use a request containing `refund_id` and `original_receipt_id`; do not overload `manual_payer_id`. Persist a small local refund-link sidecar keyed by stable refund source message ID, with target receipt ID and validated amount. It must be idempotent; an absent/stale original target must fail closed. Decide a safe filename/location consistent with existing ignored data conventions and test it.

### Adjustment calculation and result shape

- Keep `receipt.final_total_cents` as the source/evidence original total.
- Add explicit financial facts such as `original_total_cents`, `refund_adjustment_cents` (negative), and `net_total_cents` rather than overwriting source receipt total.
- Add auditable refund adjustment entries to allocation/result output, then add their negative per-participant shares to `participant_totals_cents`.
- Aggregate must use `net_total_cents`, and run/receipt Markdown must visibly state refund adjustments and net total.
- Cent split is deterministic in configured participant order using present participants only. `-1001` among five is a useful test case.
- Define all reconciliation invariants in frozen tests: adjustment shares sum to adjustment; adjusted participant totals sum to net total; run aggregate sums adjusted receipt totals; unadjusted legacy results retain current shape/behavior.

## Suggested Execution Order

1. Read the three `.agent-tasks` files, relevant source/tests, current `AGENTS.md`, and run focused existing tests. Update the task docs with exact starting state.
2. Add failing unit tests for present participant derivation, negative cent split, result net reconciliation, cached pending refund detection, persisted link validation/idempotence, and API pending-response shape. Do not modify existing assertions.
3. Implement domain/result/cache/workflow changes until focused then full suite passes. Commit a narrowly scoped backend milestone.
4. Obtain/refine refund parser fixture evidence. Prefer a sanitized fixture or a single controlled Gmail read; preserve only safe structural evidence and parsed values. Add parser tests and implementation; commit separately.
5. Only once schemas are stable, implement and visually test the UI selection flow with a fixture response; use one existing server only to avoid competing log writes.

## Do Not Drift Into

- Refund item semantic ownership, aliases, retailer/browser scraping, Gmail polling, live Splitwise writes, settlement netting, or Costco refund handling.
- Broad parser relaxation that turns non-refund malformed emails into refunds.
- Destructive Git actions, direct `main` work, credential inspection, or raw-email logging.

## Known Operational Note

A temporary second web server once triggered `PermissionError` while both processes wrote the same default `%LOCALAPPDATA%\SplitwiseFunnel\logs` file. Do not run parallel default servers. This is not the refund task unless it blocks the required later UI test.

## Immediate Next Action

Create the frozen backend tests and exact data contracts for `refund_pending_link`, persisted resolution, and negative receipt-level allocation before parsing or UI changes.
