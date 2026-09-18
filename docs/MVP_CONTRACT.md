# Receipt-to-Splitwise Funnel MVP Contract

**Target:** personal local prototype by Monday.  
**Users:** one household of 4–5 known participants.  
**Principle:** a user-authorized Gmail connection retrieves receipt emails; deterministic local code calculates money; a human approves every result.

## One end-to-end interaction

The user selects a date range and either or both vendors: `walmart_online` and `costco_same_day`. The local app queries one user-authorized Gmail inbox for configured sender-and-subject receipt signatures, then fetches the matching message bodies for the selected interval. Initial signatures are the Instacart Walmart receipt and Costco receipt emails supplied by the owner. The Gmail address, OAuth credentials, and refresh tokens are never stored in the repository or in the local JSON configuration.

The app extracts the purchase date, vendor order ID, payment-card last four digits when available, final line items, item quantities, item totals, and final receipt total from receipt HTML or a supported attachment. It stops and shows an actionable error if a required field is not visible or cannot be reconciled.

The app maps a card's last four digits to a known payer across the household; the retailer is not part of that mapping. If a new card last four appears, the local command pauses for the owner to select a known participant or create one, then atomically appends the mapping to the ignored local configuration and resumes. If a completed provider receipt does not expose the last four digits, the user explicitly selects a configured payer and the result records `payer_resolution: manual`; it never guesses. The app then removes participants absent on the purchase date, applies the fixed household rules below, splits the remaining item amount equally, and produces a copy-ready summary grouped by payer and receipt. The user may add a miscellaneous expense by providing description, payer, amount, and selected participants; absence dates do not affect it. Nothing writes to Splitwise in this MVP.

## Fixed allocation rules

1. Start each item with all job participants except people absent on the purchase date. Absence dates are inclusive and local-calendar-date based.
2. Exact raw item-description matching is case-insensitive after trimming and collapsing spaces. There is no semantic normalization, alias map, category model, or model inference.
3. An exact-item `include_only` rule replaces the eligible set. A matching `exclude` rule then removes its people. No rule may restore an absent participant.
4. If no eligible participant remains, stop the run and require an explicit rule/configuration correction.
5. Split in integer cents. Assign leftover cents in ascending configured participant-ID order. Every item and receipt must reconcile exactly.

The receipt-level residual is `final total - sum(line-item totals)`. It includes visible tax, delivery/service fees, tips, and adjustments. The MVP splits this residual equally across everyone present for the receipt, after item rules; it fails if the receipt does not label enough totals to calculate the residual.

Initial configured rules: apples only Nitish; dairy and non-vegetarian items exclude Nitish; non-vegetarian items exclude Himanshu; almonds only Krishna and Nitish. For this MVP, dairy/non-vegetarian matching is represented by explicit raw-description entries in the JSON configuration, not inferred classification.

## Local data and output

One ignored local JSON configuration file stores participants, household-wide card-last-four-to-payer mappings, absence intervals, exact-description rule entries, and the fixed participant order. Each run writes one timestamped result JSON containing retrieved order evidence, parsed line items, applied rules, allocations, and errors. It also renders a copy-ready plain-text/Markdown summary: per receipt (payer, date, items, shares) followed by per-person net totals.

## In scope and non-goals

In scope: Gmail retrieval of configured Instacart Walmart and Costco receipt emails; receipt HTML/attachment parsing; raw-item allocation; manual misc expenses; local JSON; reviewable output.

Out of scope: retailer APIs; retailer login/credential handling; browser automation, DOM scraping, cookie extraction, reverse-engineered endpoints, CAPTCHA/access-control bypass; image-only OCR; semantic grouping; refunds, returns, substitutions, cross-account duplicate detection, database storage, web UI, background jobs, and any live Splitwise write.

## Delivery gates

1. **Intake spike:** retrieve one matching receipt email from each configured signature through an authorized Gmail connection, then parse it to a normalized local fixture without changing provider data.
2. **Accounting contract:** locked tests cover absence, every initial household rule, zero-eligible failure, deterministic cents, and receipt reconciliation.
3. **End-to-end:** one fixture from each vendor produces an audited JSON result and copy-ready summary; manual review confirms the totals before use.

**Stop rule:** if either vendor's completed receipt does not expose item-level final amounts or enough totals to reconcile, record the limitation and exclude that vendor from Monday rather than inventing data or bypassing controls. A missing payment last four is handled only by an explicit manual payer selection.
