# Urology Coding Audit — System Design

**Status:** MVP built and verified (`app.py`, `cms_ingest.py`, `seed_urology.py` in this
folder). This document is the design spec the codebase implements, and the map for
extending it.

---

## 1. Product shape

A note and a list of submitted codes go in. A set of findings comes out, each one
tagged **under-coded**, **over-coded**, **needs a physician query**, or **context**,
and each one carrying a citation to a specific CMS file and a verbatim quote from the
note. Nothing is asserted without both.

Two engines produce findings, and the design keeps them structurally separate all the
way to the UI:

```
                      ┌─────────────────────┐
   note + codes  ───► │   Identifier Scrub   │
                      └──────────┬───────────┘
                                 │ clean text only
                 ┌───────────────┴────────────────┐
                 ▼                                 ▼
        ┌─────────────────┐              ┌──────────────────────┐
        │   RULES ENGINE   │              │  DOCUMENTATION REVIEW │
        │  deterministic   │              │   model judgment      │
        │  cites a CMS row │              │   cites a note quote  │
        └────────┬─────────┘              └───────────┬───────────┘
                 │                                     │
                 └──────────────┬──────────────────────┘
                                 ▼
                        findings, labeled by
                        which engine produced them
                                 │
                                 ▼
                      physician / coder review
                        agree · disagree · unsure
                                 │
                                 ▼
                         feedback store
                    (the training signal)
```

The rules engine answers questions that have one correct answer, published by CMS:
is this pair bundled, is this unit count over the cap, was this code deleted, does
this code's bilateral indicator make modifier 50 available. It never guesses.

The documentation review engine answers questions a lookup table structurally
cannot: is the medical necessity actually written down, was a service performed but
never coded at all, is there enough in the note to support the level selected. It
runs after the rules engine and is told explicitly not to repeat what rules already
covered — its entire job is the residue.

A physician or coder can always tell which kind of finding they're looking at. That
distinction is the product's credibility and it is preserved end to end: in the
finding's `kind` field, in the UI's separate "Rules layer" and "Documentation
review" sections, and in the citation shown under every finding.

---

## 2. Data layer

### 2.1 Schema

Five tables, all keyed by CPT/HCPCS code, all stamped with source URL and fetch
date:

| Table | Grain | Answers |
|---|---|---|
| `ptp_edit` | (column1, column2) pair | Is code B bundled into code A, and can a modifier bypass it |
| `mue` | code | What's the max unit count per date of service |
| `rvu` | code | Global period, bilateral indicator, multiple-procedure indicator, assistant-surgery indicator, work/PE/MP RVUs |
| `hcpcs` | code | Descriptor text for J-codes and other HCPCS Level II |
| `code_status` | code | Deleted / replaced — hand-maintained, see §2.4 |

Every row's `src` column holds the exact URL it came from and `fetched` holds the
date. A finding generated today can be traced back to the exact file version that
produced it — that's what lets a finding be defended a year later when someone asks
where it came from.

### 2.2 Ingestion (`cms_ingest.py`)

CMS republishes these files quarterly at URLs that rotate but landing pages that
don't. So the pipeline doesn't hardcode file URLs — it fetches the landing page,
finds the newest ZIP by pattern-matching link text and href, downloads it, and
parses it.

Every CMS file in this domain ships with a variable number of preamble rows before
the real header (title, effective date, a blank line, *then* column names). The
parser handles this with `header_index()` — it scans the first N rows, fuzzy-matches
normalized cell text against a wanted-column dictionary, and locks onto whichever row
looks most like a header. This is the piece that breaks first when CMS reformats a
file, and it's built to fail loud (zero rows loaded, logged to `ingest_log`) rather
than silently loading garbage.

Four sources are wired up now — PTP edits, MUEs, HCPCS, and the RVU file. Each
source is a dict entry in `SOURCES`; adding LCD/Article ingestion from the Medicare
Coverage Database bulk download is the same pattern, one more entry.

```
python3 cms_ingest.py                 # pull everything
python3 cms_ingest.py --only ptp      # pull one source
python3 cms_ingest.py --dump          # inspect a file's raw structure before writing a parser
python3 cms_ingest.py --verify        # row counts + three urology spot-checks
```

### 2.3 Starter seed (`seed_urology.py`)

A hand-verified set of ~50 urology-specific rules — the cystoscopy-bundling family,
the ureteroscopy hierarchy, MUE caps on the common codes, bilateral indicators — so
the app is usable the moment it's cloned, before the full CMS ingest has run.
Payment columns are deliberately `NULL` here; RVU values move every quarter and a
guessed dollar figure in front of a physician is worse than no dollar figure.

`app.py` reports which source is loaded (`seed` vs `CMS`) via `/status`, so it's
never ambiguous which dataset a finding came from.

### 2.4 The deleted-code table

CMS does not publish a single "codes deleted this year" file — it's reconstructed
from the CPT changebook and specialty-society summaries. This is the
highest-signal, lowest-effort table in the whole system: a practice still billing a
code that was retired six months ago is a deterministic, zero-judgment finding with
a hard dollar number attached, and it's exactly the kind of thing a billing
department misses when nobody updated the charge master.

`code_status` is hand-maintained by design. It's meant to be re-verified every
January against the new CPT release and again whenever a specialty society (AUA,
in this case) publishes its own summary — the model does not get to invent an entry
here.

---

## 3. Rules engine (`run_rules`)

Five deterministic checks, each a pure function of the codes submitted, the note
text, and a CMS table:

1. **Deleted/replaced codes** — straight lookup against `code_status`.
2. **PTP bundling** — for every ordered pair of submitted codes, look up
   `(col1, col2)`. If found, the modifier indicator decides the message: `0` means
   no modifier can ever bypass it, `1` means a modifier can bypass it *if the note
   supports a separate, distinct service*, `9` means the edit doesn't apply as
   written (e.g. add-on codes).
3. **MUE cap** — units submitted vs. the published max per date of service.
4. **Bilateral opportunity** — if the note's language suggests both sides were
   treated (keyword heuristic today; see §6 for the upgrade path) and a submitted
   code carries `bilateral_ind = 1` with no modifier 50 and only one unit, flag it
   as a candidate for the 150% bilateral adjustment.
5. **Global period context** — codes with a 10- or 90-day global period get a
   contextual note about modifiers 24/58/78/79, since staged and related
   post-op visits in that window are a common place practices write off billable
   work by mistake.

Every finding this engine produces includes `authority` (which CMS file) and
`source` (the exact stamped row's URL and fetch date) — never a paraphrase, always
a pointer to data that's sitting in the same database the person can query
themselves.

---

## 4. Documentation review (`run_review`)

A single Claude call, scoped tightly by the system prompt to *not* re-derive
anything the rules engine already owns. Its brief:

- Services described in the note that were never coded at all.
- Codes whose documentation requirements the note doesn't actually meet.
- Gaps that should become a physician query rather than a code change.
- Medical necessity that's asserted but not demonstrated.

Structural constraints baked into the prompt, not left to hope:

- Every finding must carry a verbatim quote from the note. No quote, no finding.
- Confidence is a required field, and "low" is a valid, encouraged answer.
- The prompt explicitly states the engine has no stake in the outcome direction —
  over-coding gets reported exactly as readily as under-coding. This is the
  sentence that keeps the tool from turning into a revenue-maximizer, and it's
  worth protecting verbatim in any future prompt revision.

Output is strict JSON, parsed defensively (fence-stripped, wrapped in try/except),
and every finding gets `kind: "review"` and `authority: "Model judgment — requires
coder verification"` stamped on before it ever reaches the UI. It is never
presented as if it carries the same evidentiary weight as a rules-engine finding.

---

## 5. Identifier scrub

Runs client-side-equivalent — in this build, server-side but on every keystroke via
`/scan`, before `/audit` is reachable at all. Ten pattern classes (SSN, MRN, phone,
email, date, DOB, name, address, ZIP) implemented as a straightforward regex list
in `IDENTIFIERS`, shared between `/scan` (report) and `/redact` (strip and
replace with a labeled placeholder).

`/audit` re-runs the scan server-side and refuses to proceed if anything is still
present — the UI-level block is a convenience, the server-level block is the actual
gate. This is deliberately layered ahead of the reasoning engines rather than
bolted on after: nothing generated by either engine ever has PHI in its context
window in the first place.

---

## 6. Feedback loop

Every finding renders with Agree / Disagree / Unsure buttons in the UI. This is the
part that turns a demo into a system that improves — it's also the part that isn't
wired to storage yet in this MVP (buttons currently just toggle visual state).

The next increment is a `feedback` table:

```sql
CREATE TABLE feedback (
  finding_id TEXT, kind TEXT, code TEXT, direction TEXT,
  authority TEXT, verdict TEXT,        -- agree / disagree / unsure
  reviewer_note TEXT, reviewed_by TEXT, reviewed_at TEXT,
  note_hash TEXT                        -- links back to the source note without storing it
);
```

Two things make this loop actually load-bearing rather than decorative:

- **Rules-engine disagreements are signal about the rules table, not the model.**
  If a coder disagrees with a bundling finding, that's either a documentation
  nuance the deterministic check can't see, or a stale/wrong row in `ptp_edit` —
  either way it's worth triaging by hand, because it means the CMS data itself
  needs a second look.
- **Review-engine disagreements are prompt and eval signal.** Accumulate enough of
  these and they become a held-out set for testing prompt changes before shipping
  them — the same discipline as any eval-driven LLM feature, just scoped to this
  domain.

The highest-value future signal isn't the in-session click at all — it's the 835
remittance advice that arrives weeks later showing what the payer actually paid,
denied, or adjusted. That's ground truth no amount of coder agreement can
substitute for. The `note_hash` field above exists so that link can be made later
without holding onto the note itself.

---

## 7. API surface

Five routes, stdlib `http.server`, no framework dependency:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | The UI |
| `/status` | GET | Row counts per table, whether review is enabled, seed vs. CMS source |
| `/scan` | POST | Identifier hit report for the current note text |
| `/redact` | POST | Strip identifiers, return clean text |
| `/audit` | POST | Run both engines, return combined findings |

`/audit`'s response shape:

```json
{
  "findings": [
    {"kind": "rules", "direction": "over", "code": "52000",
     "headline": "...", "detail": "...", "action": "...",
     "authority": "NCCI PTP edit, Practitioner Services",
     "source": "<url> row, fetched 2026-09-22"},
    {"kind": "review", "direction": "query", "code": null,
     "headline": "...", "detail": "...", "evidence": "<verbatim quote>",
     "action": "...", "confidence": "medium",
     "authority": "Model judgment — requires coder verification"}
  ],
  "note_review": null
}
```

`kind` is the field every downstream consumer — UI, export, future analytics —
should switch on.

---

## 8. Frontend

Single HTML page returned by `/`, vanilla JS, no build step. Two-column layout:
note and codes on the left, findings on the right, grouped into visually distinct
"Rules layer" and "Documentation review" sections. Sample charts load with one
click so the tool is usable in under ten seconds with zero data entry.

The design intentionally avoids a framework dependency at this stage — it keeps the
whole app to three files with no `npm install`, which matters more than component
reuse while the thing being validated is the reasoning quality, not the UI.

---

## 9. Extension path

Each of these slots into an existing seam rather than requiring new architecture:

**More CMS sources.** Add an entry to `SOURCES` in `cms_ingest.py`, write a loader
following the `load_ptp` / `load_mue` pattern, add a schema table. LCD/Article
ingestion from the Medicare Coverage Database bulk download is the natural next
one — it's the coverage layer sitting on top of the bundling layer that's already
built.

**More specialties.** `seed_urology.py`'s pattern — a hand-verified starter set
plus a `code_status` table for the specialty's deleted/replaced codes — is the
template. GI and urogynecology share enough procedural structure with urology
(scope-based procedures, global periods, bilateral logic) that they're the
cheapest next specialty, not a random pick.

**Payment figures.** `CONVERSION_FACTOR` in `app.py` is `None` on purpose. Wiring
it up means pulling the CY2026 conversion factor(s) from the PFS final rule —
there are two, one for APM qualifying participants and one for everyone else — and
deciding which applies per customer. Once set, `money()` turns RVUs into dollar
figures for every finding.

**Better bilateral detection.** The current heuristic is keyword-based
(`bilateral`, `both sides`, paired mentions of "right" and "left"). A structured
pass — pull anatomical laterality per procedure mention rather than per note — is
a natural target for the documentation-review engine rather than more regex.

**Modifier-25 and E/M-adjacent logic.** Not built yet. Same pattern as bilateral
detection: a rules check for the deterministic part (was an E/M code submitted
alongside a same-day procedure) plus a documentation-review check for the judgment
part (does the note actually support a separately identifiable service).

**Feedback storage and the remit join.** §6 above — this is the highest-leverage
next build, because it's the difference between a tool that answers questions and
a system that gets measurably better.

---

## 10. Deployment

Current form runs anywhere Python 3 runs — `python3 app.py` after seeding the
database, no containerization required for a single-user pilot on a practice
manager's laptop.

For a hosted pilot, the natural path:

- **Cloud Run** for `app.py` — stateless request handling maps cleanly onto it,
  and it scales to zero between uses, which matters for a low-volume early pilot.
- **Cloud SQL (Postgres)** in place of the SQLite file once more than one person
  needs to hit it concurrently — the schema in `SCHEMA` translates directly.
- **Cloud Scheduler** triggering `cms_ingest.py` quarterly, aligned to CMS's own
  posting cadence (Jan 1, Apr 1, Jul 1, Oct 1), writing to Cloud SQL instead of a
  local file.
- **Cloud Healthcare API's de-identification service** as the production-grade
  replacement for the regex-based `IDENTIFIERS` scrub, if real charts start
  flowing through rather than de-identified samples.
- **Secret Manager** for `ANTHROPIC_API_KEY` rather than an environment variable
  on a single machine.

None of this is required to keep iterating on the reasoning quality — that work
happens entirely against the local SQLite file and needs nothing hosted.

---

## 11. What's in this folder

```
coderev/
├── cms_ingest.py      # discovers, downloads, parses real CMS files → cms.db
├── seed_urology.py    # hand-verified urology starter set, no network required
├── app.py             # rules engine + review engine + UI + API, stdlib only
└── DESIGN.md           # this document
```

To run it:

```
python3 seed_urology.py                    # builds cms.db from the starter set
export ANTHROPIC_API_KEY=...                # optional — enables the review layer
python3 app.py                              # http://localhost:8000
```

To replace the starter set with the real CMS files:

```
python3 cms_ingest.py
python3 app.py
```