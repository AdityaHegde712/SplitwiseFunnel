# MVP Delivery Plan

## Objective

Deliver a local personal tool by Monday that reads selected receipt emails through a user-authorized Gmail connection, applies fixed deterministic household allocation rules, and writes an auditable JSON result plus copy-ready Markdown summary. The governing product contract is `docs/MVP_CONTRACT.md`.

## Interaction flow

`date range + vendor selection` -> Gmail query using configured receipt signatures -> fetch email HTML/allowed attachment -> vendor parser -> normalized receipt -> payer resolution -> absence/rule filtering -> cent allocation and reconciliation -> JSON evidence + Markdown summary -> manual review.

An unknown or incomplete field produces an explicit review error. Nothing calls Splitwise, logs into a retailer, automates a browser, extracts cookies, or posts an expense.

## Phases and dependencies

1. **OAuth and intake spike:** owner creates a local Google OAuth desktop client and authorizes `gmail.readonly`; implementation proves it can retrieve exactly one fixture email per vendor. Depends on owner OAuth setup.
2. **Deterministic accounting:** write frozen tests for absence, exact raw-description rules, zero-eligible failure, residual allocation, deterministic cents, and reconciliation; implement only enough accounting code to satisfy them.
3. **Parsing and output:** create vendor-specific email parsers against redacted/local fixtures, then render audited JSON and copy-ready Markdown.
4. **Smoke and handoff:** run one local end-to-end result for each vendor, inspect all totals manually, document setup and failures.

## Full execution sequence

### Phase 0: Contract and repository baseline [complete]

- Lock `docs/MVP_CONTRACT.md` as the scope authority.
- Work only on `dev`; keep `main` untouched and do not push or merge.
- Keep OAuth material outside the repository and reject any request to paste it into chat, source, configuration, or Git.

### Phase 1: Pure deterministic core [complete]

- Frozen `unittest` contracts define absence filtering, exact raw-description rules, deterministic cents, residual allocation, zero-eligible failure, and Gmail allowlist-query behavior.
- `src/domain/allocate.py` and `src/gmail/receipt_filters.py` pass all frozen tests.

### Phase 2: Configuration, receipt model, and output [complete]

- Define a non-secret `config.example.json` with participant order, card-to-payer mapping, absence intervals, and exact raw-description rules.
- Define normalized internal receipt/result contracts using integer cents only.
- Add frozen tests for configuration validation, payer resolution, renderer totals, and malformed/unsupported receipt failure.
- Implement JSON result writing and copy-ready Markdown output. Raw email HTML must not be written to the result.

### Phase 3: Gmail client and receipt parsing [complete]

- Create a local `.venv` and pin only the maintained Google OAuth/Gmail client libraries required for `gmail.readonly`.
- Implement local desktop OAuth from the owner-controlled client file path. Store the generated refresh token only beside that client file with current-user access; never log token objects, headers, raw mail, or attachments.
- Query only the configured date-bounded sender/subject allowlist. Revalidate sender and exact subject after fetching message metadata before parsing any body.
- Parse only the matching message's HTML/plain-text parts into the internal receipt model. Unsupported structure, missing final total, or unparseable money fails closed with a named diagnostic.

### Phase 4: Owner-authorized intake spike [complete]

- The owner approves the one-time system-browser Gmail consent dialog for `gmail.readonly`.
- Retrieve one selected finalized receipt per vendor, inspect only parsed fields and diagnostics, then create sanitized local fixtures that preserve the observed structural pattern without personal data.
- Extend parser tests to those fixtures; do not retain raw production emails in the repository.

### Phase 5: Local command and smoke [active]

- Add a local command that accepts date range, vendor selection, and configuration path, then writes timestamped result JSON and Markdown.
- Run a reduced two-vendor smoke through Gmail retrieval, parsing, allocation, and rendering.
- Manually reconcile every output receipt total against the extracted final total; do not write to Splitwise.
- Update setup documentation, run the complete test suite, inspect Git status, and commit the completed milestone to `dev` without pushing.

## Stop and escalation conditions

Escalate to the owner instead of guessing when: OAuth consent is required; a receipt is missing item-level final amounts; a receipt lacks a final total; an unknown payer requires manual resolution; a receipt contains a charge type outside the fixed residual policy; the Gmail signature does not match exactly; or provider email structure differs from the tested parser. The owner decides whether to supply a sanitized fixture, choose a payer, expand scope, or exclude that receipt/vendor.

## Planned paths

- `src/`: dependency-light Python domain and Gmail-filter modules; later, a Gmail client, receipt parsers, and renderers.
- `tests/spec/`: immutable Python `unittest` behavioral tests and sanitized fixtures.
- `config.example.json`: non-secret participant, payer mapping, absence, and rule shape.
- `docs/MVP_CONTRACT.md`: locked scope.

OAuth client JSON and refresh tokens stay in a per-user application-data directory outside the repository, protected to the current user. The app uses Python 3.13 with the standard-library `unittest` runner, then a maintained OAuth library with system-browser authorization, loopback redirect, and PKCE. It queries only allowlisted sender-and-subject signatures, validates those fields before parsing, and retains parsed receipt fields rather than raw mailbox bodies. No `.env` file is read, created, or inspected.

## Exit criteria

- Each supported sender/signature retrieves one completed receipt through Gmail read-only OAuth.
- Every receipt result either reconciles to its final total in cents or terminates with a named error.
- All contract tests pass unchanged after implementation.
- Summary identifies payer resolution, receipt total, each allocation, residual policy, and per-person net totals.
- No live Splitwise, retailer, or paid/cloud operation occurs.
