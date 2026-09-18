# MVP Task Ledger

## Active state

Current phase: Phase 5 local command and smoke.  
Blocker: owner must provide the private configuration path and inclusive date range for the live two-vendor smoke. Gmail authorization and the one-receipt-per-vendor parser-fixture intake are complete; no raw email body was retained.

Latest milestones: `c6d0929 feat: extract receipt payment evidence`, `fafd200 feat: add receipt processing workflow`, and `bc673dc feat: add local receipt command` on `dev`; no remote push was performed.

1. [x] Lock the MVP contract at `docs/MVP_CONTRACT.md`.
2. [x] Initialize Git with `main` and `dev`; all subsequent work is directly on `dev`.
3. [x] Owner created the Google Cloud project, enabled Gmail API, configured consent, added themselves as the sole test user, and created the Desktop OAuth client.
   - Evidence: local application authorization produced the token outside this repository; Gmail metadata and the minimum one-receipt-per-vendor structure check succeeded without retaining raw email content.
4. [x] Add frozen Gmail-query and accounting behavior tests under `tests/`.
   - Acceptance: tests cover both receipt signatures, date filtering, each household rule, residual cents, and fail-closed conditions.
   - Evidence: `python -m unittest discover -s tests/spec -v` fails on missing `src` modules, as required for the red phase.
5. [x] Implement deterministic core allocation and Gmail receipt-filter modules.
   - Evidence: `python -m unittest discover -s tests/spec -v` passed 6 tests on 2026-09-17.
6. [x] Add frozen tests and implementation for config validation, payer resolution, result JSON, and Markdown summary.
   - Acceptance: malformed config/receipt fails loudly; output shares and totals are auditable and reconcile exactly.
   - Decision: owner confirmed equal residual sharing on 2026-09-17; the frozen assertion now requires allocation-recorded residual shares to contribute to each participant's total.
7. [x] Add the local Gmail OAuth client and narrow message intake adapter.
   - Acceptance: system-browser authorization uses owner-controlled OAuth files outside Git; only verified allowlisted messages reach parsing.
   - Evidence: fake-service header/body tests and local OAuth guard tests pass; `.venv` contains the declared Google client libraries; 13 frozen tests pass.
   - Note: no Gmail consent or mailbox read occurred in this milestone.
8. [x] Add vendor parser tests using sanitized fixtures created after the owner-authorized intake spike.
   - Acceptance: one finalized receipt pattern per vendor parses to a reconciling internal receipt or fails with a named diagnostic.
   - Evidence: two sanitized fixtures from observed receipt structures pass with standard-library HTML parsing and Decimal currency conversion; 16 frozen tests pass.
9. [/] Add the local date-range command and run the two-vendor end-to-end smoke.
    - Acceptance: all frozen tests pass; result JSON and Markdown reconcile; no external writes occur.
   - Done: parse visible `Visa ending in XXXX` and `Your Visa XXXX was charged` evidence into payment method and last four; PayPal-only evidence remains explicit manual selection. Frozen suite: 17 tests pass.
   - Done: connect allowlisted Gmail messages to parsing, payer resolution, allocation, and result construction without persisting email bodies. The CLI requires local config, output path, and ISO date range; it emits one JSON audit file and a copy-ready itemized Markdown summary per receipt. Unknown Visa evidence prompts for an existing or new participant, then atomically appends a household-wide mapping. Frozen suite: 26 tests pass.
   - Active: obtain owner-provided local config and date range for the live two-vendor smoke.

## Immediate next action

Obtain the owner's private configuration path and inclusive Gmail date range, then run the two-vendor local smoke through `python -m src.cli` and inspect the generated totals.
