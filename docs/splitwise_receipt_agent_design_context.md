# Receipt-to-Splitwise Agent: Design Context and Current Decisions

**Status:** Early design / pre-implementation specification  
**Purpose:** Handoff context for coding agents. This document records the current domain model, accepted simplifications, unresolved questions, state-machine design, and immediate next work.  
**Scope:** Small-scale personal/internal tool for roughly 4–5 participants, primarily Walmart and Costco receipts, with eventual Splitwise integration.

---

## 1. Project Goal

Build a personal expense-allocation system that:

1. Accepts a date range and one or both supported retailers (`walmart`, `costco`).
2. Fetches or otherwise acquires receipts for that interval.
3. Extracts structured receipt contents.
4. Identifies the payer from the receipt payment card last four digits.
5. Normalizes semantically equivalent item descriptions.
6. Applies participant absence windows.
7. Applies explicit item/category-specific allocation rules.
8. Deterministically calculates each participant's share.
9. Allows manual miscellaneous expenses.
10. Presents the full result for review.
11. Eventually syncs the approved result into Splitwise.

Core design principle:

> **AI interprets; deterministic software accounts.**

AI/semantic tooling may help interpret receipt text and normalize products, but monetary arithmetic, rule application, splitting, rounding, reconciliation, and external writes should be deterministic.

---

# 2. Current Domain Model

The entities below are the current working model. Fields marked **OPEN** are not yet final.

## 2.1 Participant

Represents one person who may share expenses.

```text
Participant
├── participant_id
├── display_name
├── status            # OPEN: may be unnecessary for MVP
└── external_ids
    └── splitwise_user_id
```

### Current decisions

- `participant_id` should be stable and human-readable.
- Preferred format:

```text
participant_<name>
```

Example:

```text
participant_nitish
participant_krishna
participant_himanshu
```

- Names should not be used as the primary key.

### Pending clarification: `status`

The original proposal included `status`, intended to support values such as:

```text
active
inactive
```

This would allow a participant to remain referenced by historical data while being excluded from new jobs.

However, because this is a small personal MVP for 4–5 people, **`status` may be unnecessary and should be considered removable unless a concrete use case emerges.**

---

## 2.2 PaymentInstrument

Represents a payment card and its owner.

```text
PaymentInstrument
├── payment_instrument_id
├── owner_participant_id
├── retailer
└── last_four
```

### Current decisions

The system is intentionally small-scale and primarily uses US debit/credit cards. Do not over-generalize payment-instrument modeling.

The practical identification tuple is:

```text
retailer + last_four
```

Example:

```text
costco + 4821 -> participant_krishna
walmart + 7316 -> participant_nitish
```

No need for issuer, card network, global uniqueness assumptions, or a generalized payment-method taxonomy in the MVP.

`payment_instrument_id` can be internally derived or explicitly assigned, e.g.:

```text
payment_costco_4821
payment_walmart_7316
```

---

## 2.3 Absence

Represents an interval during which a participant should normally be excluded from purchases.

```text
Absence
├── absence_id
├── participant_id
├── starts_at
├── ends_at
└── note
```

### Current decision

Keep absence periods as independent records because each participant may have multiple intervals.

Exact date/time boundary semantics will be defined later.

---

## 2.4 ReceiptSource

Represents the original evidence from which a receipt is extracted.

```text
ReceiptSource
├── source_id
├── source_type
├── retailer
├── acquired_at
├── original_uri_or_path
├── checksum
└── acquisition_metadata
```

Possible source types may include:

```text
WALMART_ACCOUNT
COSTCO_ACCOUNT
EMAIL
PDF_UPLOAD
IMAGE_UPLOAD
MANUAL
```

### Current decisions

- Preserve the original source independently from the structured `Receipt`.
- `checksum` can assist with deduplication.
- Receipt acquisition methodology is not yet decided.

---

## 2.5 Receipt

Represents the structured transaction extracted from a receipt source.

### Simplified MVP shape

```text
Receipt
├── receipt_id
├── retailer
├── transaction_datetime
├── source_id
├── external_transaction_id    # optional, if retailer provides one
├── payment_last_four
├── line_items[]
├── receipt_level_adjustments[]
├── totals
└── extraction_metadata
```

### Current decisions

- Remove `store_id` from the MVP.
- `retailer` should simply identify `walmart` or `costco`.
- `source` should not contain an arbitrary object. Use `source_id` to reference the associated `ReceiptSource`.
- The previously proposed `payment_instrument_reference` is simplified:
  - the receipt records `payment_last_four` exactly as extracted;
  - the system resolves `(retailer, payment_last_four)` against the `PaymentInstrument` registry;
  - the resolved owner becomes the payer for allocation purposes.

### Pending clarification: `receipt_id`

Recommended MVP behavior:

- **Generate `receipt_id` internally.**
- Preserve retailer-provided transaction/order identifiers separately as `external_transaction_id` when available.

Reasoning:

- Walmart and Costco may expose different identifier formats.
- Some ingestion sources may omit retailer transaction IDs.
- An internal ID gives every receipt a stable system identity regardless of acquisition method.

Potential format:

```text
receipt_<uuid>
```

or another deterministic/internal identifier.

This is **proposed but not yet formally locked.**

---

## 2.6 LineItem

Represents an item exactly as shown by the retailer transaction.

```text
LineItem
├── line_item_id
├── raw_description
├── quantity
├── unit_price
├── gross_amount
├── discounts[]
├── net_amount
└── source_position
```

### Current decisions

- Preserve the retailer's raw description permanently.
- Semantic normalization must never overwrite `raw_description`.
- Exact treatment of weighted products, coupons, tax, deposits, etc. remains future work.

---

## 2.7 NormalizedItem

Represents the semantic interpretation of a `LineItem`.

### Revised proposed shape

```text
NormalizedItem
├── line_item_id
├── canonical_name
├── categories[]
├── is_dairy
├── is_nonveg
├── attributes[]       # OPEN: likely removable for MVP
├── confidence         # OPEN: do not rely on until methodology is defined
├── interpretation_source
└── user_corrected
```

Example:

```text
raw_description = "ORG BNNA 3LB"
canonical_name = "banana"
categories = ["fruit", "produce"]
is_dairy = false
is_nonveg = false
```

### Current decisions

Add explicit:

```text
is_dairy: boolean
is_nonveg: boolean
```

These properties exist specifically because current household rules depend on dairy and non-vegetarian classifications.

### Pending clarification: `attributes`

Originally intended as optional semantic descriptors not worth making first-class fields, for example:

```text
organic
frozen
prepared
bulk
snack
```

No current rule depends on them. **Strong candidate for removal from the MVP unless a concrete rule requires them.**

### Pending clarification: `confidence`

Originally intended to represent how certain an extraction/normalization system is about a semantic interpretation.

Example:

```text
"ORG BNNA" -> banana
confidence = 0.97
```

However, no confidence methodology has been chosen, and arbitrary model-reported confidence values should not control financial logic.

**Current decision:**

- Treat all unresolved uncertainty as blocking.
- Do not design blocking/non-blocking thresholds yet.
- `confidence` may remain absent from the MVP schema until there is a defensible way to produce it.

### Pending clarification: `user_corrected`

Intended to be a boolean:

```text
false = interpretation produced automatically
true  = user manually changed/confirmed the semantic interpretation
```

Possible future extension:

```text
correction_source
corrected_at
previous_value
```

These extensions are not required for the MVP.

---

## 2.8 AllocationRule

This entity needs further design before implementation. The earlier description was too abstract.

The purpose of an `AllocationRule` is:

> Match an item or category and modify the set of participants who should share that item.

It should **not** directly calculate money. It determines allocation eligibility; deterministic accounting code performs the monetary split afterward.

### More concrete preliminary shape

```text
AllocationRule
├── rule_id
├── name
├── enabled
├── match
│   ├── canonical_names[]      # optional
│   ├── categories[]           # optional
│   ├── is_dairy               # optional
│   └── is_nonveg              # optional
├── action
│   ├── include_only[]         # optional
│   └── exclude[]              # optional
└── priority / precedence      # OPEN
```

Examples:

### Nitish gets all apples

```text
rule_id: rule_nitish_apples
match:
  canonical_names: [apple]
action:
  include_only: [participant_nitish]
```

### Nitish gets no dairy

```text
rule_id: rule_nitish_no_dairy
match:
  is_dairy: true
action:
  exclude: [participant_nitish]
```

### Himanshu gets no non-vegetarian products

```text
rule_id: rule_himanshu_no_nonveg
match:
  is_nonveg: true
action:
  exclude: [participant_himanshu]
```

### Almonds only belong to Krishna and Nitish

```text
rule_id: rule_almonds_krishna_nitish
match:
  canonical_names: [almond]
action:
  include_only:
    - participant_krishna
    - participant_nitish
```

### Still unresolved

Before implementation, define:

- whether multiple rules may match one item;
- precedence between `include_only` and `exclude`;
- whether rules are applied sequentially or composed from a base participant set;
- conflict behavior;
- whether rules can override absence filtering;
- whether priority is needed at all for the MVP;
- category vs exact-item matching semantics;
- singular/plural/canonical-name matching details.

This is one of the highest-priority design tasks.

---

## 2.9 Allocation

Represents the final deterministic accounting result for one line item.

```text
Allocation
├── allocation_id
├── line_item_id
├── eligible_participants[]
├── excluded_participants[]
├── applied_rules[]
├── shares
└── explanation
```

Example:

```text
item: almonds
amount: $12.00
eligible_participants:
  - participant_krishna
  - participant_nitish
shares:
  participant_krishna: $6.00
  participant_nitish: $6.00
applied_rules:
  - rule_almonds_krishna_nitish
```

### Current decision

The allocation should preserve enough information to explain why every participant owes the amount they do.

---

## 2.10 MiscExpense

Represents manually entered non-receipt expenses.

```text
MiscExpense
├── expense_id
├── description
├── payer_id
├── amount
├── participant_ids[]
└── created_at
```

### Current decisions

- Explicitly selected participants determine sharing.
- Absence windows do not automatically alter a miscellaneous expense because the user is directly selecting who participated.
- The payer may also be a participant in the expense.

---

## 2.11 Job

Represents one complete requested accounting run.

```text
Job
├── job_id
├── created_at
├── requested_date_range
├── retailers[]
├── participant_scope[]
├── status
├── receipt_ids[]
├── misc_expense_ids[]
├── configuration
└── results
```

### Current decisions

- The earlier timestamp-named JSON concept should become a serialization/output of a `Job`, rather than the JSON file itself defining the domain model.
- Exact persistent-storage strategy is not yet decided.

---

# 3. Explicitly Removed / Deferred Entity Complexity

## RuleSetSnapshot

**Removed from MVP.**

Reason:

- Household rules are expected to remain stable.
- Versioning rule sets adds unnecessary complexity for the near-term personal MVP.
- This can be revisited later if historical rule changes become a real requirement.

---

# 4. Current Relationships

```text
Participant
    ├── PaymentInstrument
    └── Absence

ReceiptSource
    ↓
Receipt
    ├── LineItem
    │      ↓
    │   NormalizedItem
    │      ↓
    │   Allocation
    │
    └── Receipt-level adjustments

AllocationRule
       ↓
Allocation

Job
├── Receipt[]
├── MiscExpense[]
├── Participant scope
└── Allocation results
```

---

# 5. Current End-to-End Job State Machine

The current working lifecycle is:

```text
CREATED
   ↓
CONFIGURED
   ↓
ACQUIRING_RECEIPTS
   ↓
RECEIPTS_ACQUIRED
   ↓
EXTRACTING
   ↓
NORMALIZING
   ↓
RESOLVING_AMBIGUITIES
   ↓
READY_FOR_ALLOCATION
   ↓
ALLOCATING
   ↓
REVIEW_REQUIRED
   ↓
APPROVED
   ↓
SYNCING
   ↓
COMPLETED
```

Error/failure branches will be designed later.

---

## 5.1 CREATED

A job exists but is not yet fully configured.

Minimum known state:

```text
job_id
created_at
```

---

## 5.2 CONFIGURED

Required job inputs are known:

```text
date range
retailers
participants
absence periods
```

Example validations:

```text
start_date <= end_date
at least one retailer selected
at least one participant selected
```

---

## 5.3 ACQUIRING_RECEIPTS

System retrieves relevant receipt sources.

Receipt acquisition methodology is unresolved.

---

## 5.4 RECEIPTS_ACQUIRED

Original receipt evidence is now available.

Deduplication should happen before downstream accounting.

Exact deduplication methodology remains unresolved.

---

## 5.5 EXTRACTING

Receipt sources are converted into structured `Receipt` data.

Important invariant candidate:

> Extraction must not perform participant allocation.

Example output:

```text
transaction metadata
payment last four
line items
subtotal
discounts/tax/other receipt adjustments
total
```

---

## 5.6 NORMALIZING

Raw line-item descriptions are mapped into semantic products/categories.

Example:

```text
"ORG BNNA 3LB"
    ↓
canonical_name = banana
is_dairy = false
is_nonveg = false
```

Any ambiguity currently blocks progress until resolved.

---

## 5.7 RESOLVING_AMBIGUITIES

Human-in-the-loop resolution stage.

Potential blocking issues include:

```text
unknown card / payer
uncertain product normalization
missing purchase date
unreadable item price
receipt total mismatch
duplicate ambiguity
```

### Current policy

**Treat every unresolved ambiguity as blocking for now.**

Do not introduce confidence thresholds or non-blocking warnings yet.

Ideally collect multiple unresolved issues and present them together rather than interrupting the workflow one-by-one.

---

## 5.8 READY_FOR_ALLOCATION

All required accounting inputs are known:

```text
valid receipts
resolved payer
normalized items
participant list
absence periods
allocation rules
```

This marks the boundary after which financial calculations should be deterministic.

---

## 5.9 ALLOCATING

For each receipt:

```text
start with job participants
↓
remove participants absent on purchase date
↓
for each line item:
    match allocation rules
    ↓
    determine final eligible participant set
    ↓
    split line-item amount deterministically
    ↓
    deterministically handle remainder cents
    ↓
    persist Allocation + explanation
```

Receipt totals must reconcile before the workflow can proceed.

Exact handling of tax, discounts, coupons, deposits, and other receipt-level adjustments is still unresolved.

---

## 5.10 REVIEW_REQUIRED

System presents the complete proposed result before any Splitwise write.

Review should include, at minimum:

```text
receipts
payer per receipt
line items
normalized interpretations
applied allocation rules
participant shares
totals
manual miscellaneous expenses
```

User corrections should invalidate only the necessary downstream computations.

Example:

```text
change normalized item:
banana -> almond

then re-run allocation for the affected dependency chain
```

Original `ReceiptSource` evidence remains unchanged.

---

## 5.11 APPROVED

User explicitly approves the computed accounting result.

No Splitwise writes should occur before this state.

---

## 5.12 SYNCING

Approved results are converted into Splitwise operations.

Still to design:

```text
idempotency
retry behavior
external Splitwise IDs
partial failure handling
duplicate-write prevention
Splitwise expense granularity
```

---

## 5.13 COMPLETED

All intended Splitwise writes have succeeded.

The job stores the resulting external identifiers and final result.

Revision/amendment semantics are deferred until later.

---

# 6. Deliberately Deferred Decisions

The following should **not** be guessed by an implementation agent yet.

## Receipt acquisition

Need to determine how Walmart and Costco receipts will actually be fetched:

- retailer APIs, if usable;
- authenticated account scraping/browser automation;
- emails;
- uploaded PDFs/images;
- mixed ingestion strategy.

## Deduplication

Need a deterministic strategy for recognizing the same receipt across ingestion methods.

## Receipt accounting details

Need rules for:

- sales tax;
- discounts/coupons;
- deposits;
- refunds/returns;
- weighted items;
- quantity handling;
- receipt-level vs line-level adjustments;
- rounding.

## Item normalization

Need to determine:

- normalization methodology/tooling;
- canonical-name vocabulary;
- whether normalization results are cached/reused;
- whether user corrections become persistent mappings;
- whether `attributes` exists;
- whether confidence scoring exists at all.

## AllocationRule semantics

Need formal rule matching and conflict-resolution semantics before implementation.

## Absence semantics

Need to decide:

- inclusive/exclusive date boundaries;
- date vs datetime representation;
- timezone;
- whether item-specific rules can ever override absence.

## Persistence

Need to choose storage strategy for:

- participant/card registry;
- absence records;
- rules;
- receipt sources;
- normalized receipts;
- jobs/results;
- manual corrections.

Do not assume that every entity needs a database table for the MVP.

## Splitwise synchronization

Need to determine:

- one Splitwise expense per receipt vs aggregated expenses;
- descriptions/details attached to expenses;
- payer/share representation;
- idempotency keys;
- retry/recovery flow;
- approval UX.

---

# 7. Immediate Next Task

**Do not implement schemas yet.**

The immediate next design task is:

## Domain vocabulary + key invariants table

For every accepted entity/concept, define:

```text
Concept
Definition
Authoritative source
Mutable vs immutable
Key invariants
Required relationships
```

Example structure only:

| Concept | Definition | Key invariant |
|---|---|---|
| ReceiptSource | Original receipt evidence | Never mutated |
| Receipt | Structured retail transaction | Totals must reconcile |
| LineItem | Retailer-recorded purchase line | Raw description preserved |
| NormalizedItem | Semantic interpretation | Does not overwrite raw source |
| Allocation | Computed expense ownership | Shares sum exactly to allocated amount |

The table itself has **not yet been finalized** and is the next task.

---

# 8. Subsequent Design Tasks

After the vocabulary/invariants pass, proceed roughly in this order:

1. **Formalize AllocationRule semantics**
   - exact match model;
   - action types;
   - precedence;
   - conflicts;
   - interaction with absence.

2. **Formalize the allocation function**

```text
LineItem
+ NormalizedItem
+ Participants
+ Absences
+ AllocationRules
        ↓
Allocation
```

Define exact inputs, outputs, invariants, and rounding.

3. **Define receipt/line-item monetary reconciliation**
   - subtotal;
   - discounts;
   - tax;
   - receipt-level adjustments;
   - exact-cent reconciliation.

4. **Define canonical JSON/data schemas** only after the semantics above are stable.

5. **Choose receipt acquisition tooling** for Walmart and Costco.

6. **Choose normalization methodology/tooling.**

7. **Define persistence layout.**

8. **Define review/correction behavior and dependency invalidation.**

9. **Define Splitwise adapter and idempotent synchronization contract.**

10. **Create representative test fixtures and acceptance criteria**, including tricky receipts and rule interactions.

---

# 9. Guidance for Coding Agents

Until the unresolved design items above are resolved:

- Do **not** invent additional scalability layers.
- Do **not** generalize the application for arbitrary retailers/users unless required.
- Target the actual use case: one personal household, ~4–5 participants, Walmart + Costco.
- Do **not** use LLM output directly as monetary truth.
- Do **not** let receipt parsing perform expense allocation.
- Do **not** overwrite original receipt evidence or raw line-item text.
- Do **not** silently resolve unknown payers, uncertain items, mismatched totals, or ambiguous duplicates.
- Treat unresolved ambiguity as blocking until the user explicitly defines a softer policy.
- Do **not** implement `RuleSetSnapshot` for the MVP.
- Avoid adding schema fields solely for hypothetical future scalability.
- Preserve explainability: every participant share should be traceable to the source item, applicable absence filtering, matched rules, and deterministic split.

---

## Current milestone

**Completed:**

- Initial project interpretation
- First-pass domain entity identification
- Small-scale scope correction
- First-pass entity relationships
- First-pass job lifecycle/state machine
- Deterministic-accounting boundary
- Removal of `RuleSetSnapshot` from MVP
- Initial clarification of `AllocationRule` intent

**Next:**

> Build the domain vocabulary + invariants table, then formally specify AllocationRule semantics and the allocation function.
