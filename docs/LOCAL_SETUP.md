# Local Setup

## Owner-controlled Gmail OAuth

Create the local virtual environment and install the tracked dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Place the downloaded Google OAuth desktop-client file at:

```text
C:\Users\hifia\AppData\Local\SplitwiseFunnel\oauth\credentials.json
```

The application will create its refresh-token file in the same owner-only directory after the owner approves the system-browser `gmail.readonly` consent flow. Neither file belongs in this repository, local JSON configuration, logs, test fixtures, or chat.

## Household configuration

Copy `config.example.json` to `data/household.json` or another ignored local location, replace the placeholder card last four digits, and add all household-wide Visa last-four-to-payer mappings. The parser recognizes both `Visa ending in XXXX` and `Your Visa XXXX was charged`. An unknown Visa pauses the command so the owner can select a known person or create one; the choice is appended atomically to this local configuration. A PayPal-only receipt is labeled as PayPal but has no card mapping key, so choose its payer explicitly. Add absence ranges as ISO dates. Rules match only an exact raw item description after trimming, collapsing spaces, and case-folding.

The app treats `final_total_cents - item_total_cents` as the receipt-level residual and shares it among people present on the purchase date. It stops rather than guessing when an item has no eligible participant, a payer is unknown, or a receipt cannot reconcile.

## Run a receipt range

Choose a private output directory outside this repository. The command reads only the two allowlisted receipt signatures and writes one JSON audit record plus one copy-ready Markdown summary for every matched receipt:

```powershell
.\.venv\Scripts\python.exe -m src.cli `
  --config C:\private\splitwise-household.json `
  --output C:\private\splitwise-results `
  --start-on 2026-09-01 `
  --end-on 2026-09-17
```

To limit the run, append `--vendors walmart_online` or `--vendors costco_same_day`. When a receipt has PayPal-only or otherwise unmapped payment evidence, append `--manual-payer <configured-participant-id>` for that run. The command never writes to Gmail, a retailer, or Splitwise.
