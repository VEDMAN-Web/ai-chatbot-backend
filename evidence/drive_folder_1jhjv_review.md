# Review — Google Drive folder `1jhjvGFkpSYkYR2_Ti6USE6J0nD3snfJ3`

Run: 2026-10-31 · Draft-only (nothing written to Postgres or Firestore) · Full data with per-field evidence:
`drive_folder_1jhjv_review.json`

How it was run: the folder is shared "anyone with the link", so its 4 files were downloaded without credentials and fed
through the ingestion pipeline (format checks → extraction → drafts → validation). Scanned pages were read with
Claude vision OCR; unstructured text with the evidence-checked Claude extraction (every value must be quoted from the
document). The configured database and Firestore were blocked during the run.

## Files

| File | Pages | How it was read | Status | Drafts |
|---|---|---|---|---|
| Brouchar.pdf | 24 | scanned → OCR (23 of 24 pages returned text) | partially_completed | 1 |
| KALHAR RESIDENCY PRESENTATION 11.03.2026.pdf | 5 | text layer (architectural plans) | completed | 1 |
| Suryoday Hills-3.pdf | 12 | scanned → OCR (12/12) | completed | 1 |
| Varsovia_Estate_Property_Aileen_Residence_Bangtao_20261030_142552.pdf | 8 | text layer | completed | 1 |

## Drafts (all need review before saving)

| Property | Found in the documents | Still needed before it can be saved |
|---|---|---|
| **Aileen Residence Bangtao** (ID `PROP-PHK-001`) | Villa · Under construction · Bang Tao, Phuket, Thailand · 2–3 bedrooms · 3 bathrooms · private pool · Leasehold/Freehold · developer Aileen Residence · "54 villas, completion Q4 2027" | **Price** — the brochure says `from 8.xx million THB` (a template placeholder, not a price) |
| **SURYODAY HILLS** | Apartment (2 & 3 BHK flats & shops) · Swami Vivekananda Chowk, Behind Devasya School, Vastral, Ahmedabad, Gujarat 380038 · developer Mishva Infra · amenities: kids play area, sit out, PNG line, 24×7 security, fire safety | **Price**, **status** (not stated in the brochure) |
| **AYODHYAPURAM** (by Radhe) | 3 BHK apartments · amenities: swimming pool, landscaped garden, box cricket, badminton court, children's play area, terrace garden … | **Location/province**, **price**, **status** (not in the readable pages) |
| **KALHAR RESIDENCY** | Floor plans for blocks A, B, C · club house · ground-floor shops | **Property type**, **location**, **price**, **status** (the PDF is a plan set; the address on page 1 belongs to the architect's office, so it was not used) |

Notes for the reviewer
* `PROP-PHK-001` in the Aileen brochure is the id used by the internal sample data. Confirm it is the real listing id
  before saving, or change it in review.
* Suryoday Hills is in Ahmedabad, India. The Ayodhyapuram and Kalhar Residency documents do not state the
  project location. Listings store THB prices, so enter price and currency deliberately in review.
* No value was invented. Missing fields stay empty and keep the drafts in **manual review**.

## How to finish (in the deployed admin)

1. Paste the folder link in the admin chat (or `POST /api/v1/admin/drive-ingestion/jobs`) with Google Workspace
   connected; the same drafts are produced.
2. For each draft, add the missing values: `PATCH /api/v1/admin/drive-ingestion/jobs/{job_id}/drafts/{draft_id}`
   with `{"fields": {...}, "action": "create"}`.
3. Approve: say **approve import** then **confirm**, or `POST …/approve` with `{"all_ready": true, "confirm": true}`.
   Each saved listing gets a 6-day undo operation id; `POST …/rollback` undoes the whole import.
