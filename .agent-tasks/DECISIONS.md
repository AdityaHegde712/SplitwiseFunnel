# MVP Decisions

## Accepted

1. **Email is the receipt source.** Query the owner-authorized Gmail inbox for the supplied Instacart-Walmart and Costco receipt signatures. This avoids retailer site automation.
2. **OAuth is local and read-only.** Use a Google OAuth desktop-app client with only `https://www.googleapis.com/auth/gmail.readonly`, system-browser authorization, loopback redirect, and PKCE. The owner creates and authorizes it; client credentials and refresh tokens are outside Git and restricted to the current user.
3. **Money is deterministic.** Store and calculate all amounts as integer cents. Raw receipt evidence is preserved; parsed output never silently invents missing values.
4. **Rules are narrow.** Match normalized raw descriptions only by case-insensitive trim/space collapse. No LLM, alias map, category taxonomy, or semantic normalization in MVP.
5. **Residual policy is fixed.** `final total - item total sum` is split equally across present participants. Items use absence filtering plus exact rules; a zero-eligible item fails closed.
6. **Mailbox minimization is enforced in code.** Gmail has no receipt-only read scope. The implementation queries and validates the configured sender/subject allowlist before parsing, fetches only selected matching messages, and does not retain raw mail content.
7. **OAuth lifecycle is explicit.** The owner adds their account as the sole test user during setup. After proving the local flow, the owner publishes the self-only app to Production because Testing-mode refresh grants expire after seven days.
8. **Production email is transient.** The local app retains parsed receipt evidence needed for the result, never raw Gmail body HTML, MIME payloads, OAuth headers, tokens, or attachments. Sanitized parser fixtures are the only email-shaped artifacts allowed in Git.
9. **Runtime and dependency boundary.** Use Python 3.13, standard-library `unittest`, and a local `.venv`. Add only maintained Google OAuth/Gmail client libraries for the Gmail adapter; accounting and rendering remain standard-library code.
10. **Payment evidence is parsed, not guessed.** Receipt parsers extract a normalized payment method and card last four from visible payment text when available. Payer lookup is household-wide `last_four -> participant`, independent of retailer. A new Visa pauses for an owner selection and atomically appends the local mapping; a payment method without last four, including PayPal-only evidence, requires explicit manual payer selection.

## Deferred

- Splitwise writes, persistence beyond result JSON, semantic grouping, OCR, retailer automation, refunds/returns/substitutions, and multi-account deduplication.

## Rejected

- Playwright or hidden-endpoint receipt harvesting: fragile and inconsistent with Walmart's published restriction on automated site retrieval.
- A service account: inappropriate for a personal consumer Gmail inbox; it does not replace user OAuth consent.
