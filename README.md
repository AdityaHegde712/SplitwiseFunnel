# Splitwise Funnel

Local, review-first household receipt splitting for Instacart Walmart and Costco Same-Day orders. It reads only allowlisted Gmail receipt emails, calculates deterministic cent-exact splits, and produces copy-ready summaries for manual entry into Splitwise.

## Current capabilities

- Gmail read-only OAuth with exact sender-and-subject validation for the two supported receipt types.
- Receipt parsing for tested finalized email structures, including Visa last-four and PayPal evidence.
- Historical receipt ingestion cache: each exact date-range/vendor selection is fetched from Gmail once, parsed into ignored local storage, and reused for payer decisions and reruns without further Gmail API calls.
- Household-wide card-last-four-to-payer mappings in ignored local configuration.
- Inclusive absence dates, exact raw-item rules, deterministic residual allocation, and audit JSON plus Markdown output.
- Each completed receipt range includes a copy-ready run total: household spend and each participant's gross share across all selected receipts.
- Unknown Visa handling: choose an existing participant or create one; the local mapping is atomically updated. PayPal-only receipts require a manual payer choice.
- Local loopback UI with Run Receipts, Past Transactions, and Household Setup tabs.
- Verbose, rotating local structured logs with correlation IDs and sensitive-field redaction.

## Boundaries

- No retailer browser automation, retailer API use, cookie handling, OCR, or live Splitwise writes.
- No credentials, Gmail bodies, OAuth tokens, or local household mappings are committed.
- Parsed local receipt caches are ignored. They retain only parsed receipt fields and safe source metadata, never raw email bodies.
- Semantic item grouping, refunds, returns, substitutions, and duplicate detection are not yet implemented.

## Local configuration

Copy `config.example.json` to an ignored location such as `data/household.json`. The relevant shape is:

```json
{
  "participant_ids": ["aditya_hegde", "example_housemate"],
  "payment_mappings": [
    {"last_four": "1234", "payer_id": "aditya_hegde"}
  ],
  "absences": [],
  "rules": []
}
```

See [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) for OAuth and command-line setup.

## Run the local UI

With the virtual environment prepared and local config in `data/household.json`, run:

```powershell
.\.venv\Scripts\python.exe -m src.web.server
```

Open `http://127.0.0.1:8765`. The server binds only to your computer; the interface does not write to Splitwise. Local operational logs are written to `%LOCALAPPDATA%\SplitwiseFunnel\logs`.

The first run for an exact selection fetches Gmail and saves a local parsed-receipt cache under ignored `data/receipt_ingestions/`. Repeating that selection, including after a payer decision, reuses the cache and makes no Gmail request. Changing either date or the selected vendors intentionally creates a new ingestion selection.

The review screen puts the aggregate run total above individual receipt summaries. It is the gross allocation across that selected run, not a calculated settlement transfer between people who paid different receipts.

## Discover Instacart Walmart receipt schemas

To inventory the last eight months of supported Instacart Walmart receipt layouts before extending parsers, run:

```powershell
.\.venv\Scripts\python.exe -m src.diagnostics.discover_instacart_schemas --batch-size 10 --pause-seconds 3
```

The command makes one narrow Gmail query per supported Instacart receipt subject, then downloads and analyzes receipts in paced batches. It writes a resumable message-id inventory under ignored `data/schema_discovery/instacart_walmart/raw/` and schema-only reports under `processed/`. The reports exclude raw email bodies, product names, totals, and payment details. If Gmail rate-limits a batch, wait and run the same command again; already analyzed messages are not queried again.
