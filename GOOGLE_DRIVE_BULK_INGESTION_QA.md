# Google Drive Bulk Ingestion — All File Formats: Implementation & QA

Status date: 2026-10-31 · Branch: `rp-user-fix`

## 0. Summary and honest status

| Item | Status |
|---|---|
| Folder URL intake (admin chat + REST), folder validation with the admin's own Google OAuth (`drive.readonly`) | Implemented, tested against a simulated Drive |
| Paginated, recursive discovery with depth / file-count limits | Implemented, tested |
| Format detection (Drive MIME + extension + byte signature) and one extractor per format | Implemented, tested with real generated files of every format |
| Batches (default 10, configurable 1–25), per-file and per-batch status, progress (polling + SSE) | Implemented, tested |
| Retry (failed files only), resume after interruption, idempotent job creation, deterministic draft ids, idempotent approval | Implemented, tested |
| Unified property drafts with per-field evidence, source reference and confidence; no invented values | Implemented, tested |
| Duplicates across files/formats, conflicts, match against existing Postgres + Firestore listings, validation | Implemented, tested |
| Review data, edits, explicit approval → Postgres + Firestore sync, 6-day undo snapshot, audit log, rollback | Implemented, tested (SQLite + fake Firestore) |
| OCR for scanned PDFs / images (Claude vision) | Implemented; **off by default**. Tested with a stubbed OCR function only — **live Claude OCR NOT EXECUTED** |
| Evidence-checked Claude extraction for unstructured documents | Implemented; tested with a stubbed model response — **live Claude call NOT EXECUTED** |
| Live Google Drive, live Firestore, live Postgres | **NOT EXECUTED** (no production/staging data was touched; see §9) |
| Admin panel review *screen* (frontend) | **Not in this repository.** The backend provides the review API (§5) the screen needs |

> **Important finding:** the original "Google Drive Folder Bulk Ingestion" implementation referenced by the
> requirement did **not exist** in this repository (on any branch). Only single-file Drive reads
> (`drive_service.fetch_drive_file`) and the batch-file proposal/undo flow existed. This work therefore builds the
> folder pipeline **on top of** those existing pieces instead of duplicating them: Drive OAuth (`google_manager`),
> `PendingAction` confirm flow, `save_ingested_listing` → `rest_create_property` / `rest_update_property`
> (validation, Postgres + Firestore, undo snapshot, audit) and `undo_batch_property_operation`. No existing API or
> behaviour was removed or changed (see §8 for the only edits to existing files).

---

## 1. Architecture

```
Admin chat ("import … <folder URL>")  ─┐
REST  POST /admin/drive-ingestion/jobs ─┴─► drive_folder_ingest.create_job
                                            ├─ parse/validate folder (admin's own OAuth, read-only)
                                            ├─ discover files (pagination, recursion, limits) → file records (Firestore)
                                            └─ background runner (thread; lease for multi-instance safety)
                                                 for each batch (default 10 files):
                                                   for each file (isolated, time-limited, 2 workers):
                                                     download / export (size-capped stream)
                                                     drive_formats.extract_bytes  (signature check → format extractor)
                                                     build_drafts  (structured rows + label pairs + evidence-checked LLM)
                                                 consolidate_job (duplicates, conflicts, existing match, validation, action)
Review:  GET drafts · PATCH draft · POST approve(confirm=true) ─► save_ingested_listing (existing write path)
Chat:    "approve import" ─► PendingAction(approve_drive_ingestion) ─► existing Confirm ─► approve_drafts
Rollback: POST rollback(confirm=true) ─► undo_batch_property_operation per saved draft
```

| File | Role |
|---|---|
| `app/services/drive_formats.py` | MIME/extension classification, byte-signature checks, extractors for every format, ZIP safety, limits, deadlines |
| `app/services/drive_ocr.py` | Claude vision OCR (opt-in) |
| `app/services/drive_folder_ingest.py` | Folder parsing, Drive access, discovery, Firestore job store, runner, retry/resume, drafts, consolidation, review, approval, rollback, views |
| `app/services/admin_drive_ingest_chat.py` | Admin chat commands |
| `app/api/v1/drive_ingestion.py` | REST API |
| `tests/test_drive_formats.py`, `tests/test_drive_folder_ingestion.py`, `tests/test_drive_ingest_helpers.py` | Automated tests and in-memory file builders |

Firestore collections: `drive_ingestion_jobs`, `drive_ingestion_files` (`{job_id}__{drive_file_id}`), `drive_ingestion_drafts`.
If Firestore is not configured, job creation is refused with HTTP 503 (jobs must survive restarts); an in-memory store
is used only under tests or when `DRIVE_INGEST_ALLOW_MEMORY_STORE=true` (dev).

---

## 2. Supported formats and MIME types

| Format | Extensions | MIME types (Drive) | Processing | Source reference kept |
|---|---|---|---|---|
| PDF | `.pdf` | `application/pdf` | pypdf text per page; image-only pages → OCR when enabled, otherwise `needs_ocr`/`partial`; encrypted → `encrypted` | page (+ `ocr: true`) |
| Word | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | body in order: headings, paragraphs, tables (header row → records), headers, footers; embedded images listed | paragraph, section heading, table, row |
| Word 97-2003 | `.doc` | `application/msword` | **Unsupported** (reason: save as .docx) | — |
| Google Docs | native | `application/vnd.google-apps.document` | exported as .docx, parsed like Word | same as Word |
| Excel | `.xlsx` | `…spreadsheetml.sheet` | every worksheet; header-row detection; one record per row; cached values (formulas never evaluated) | sheet, row |
| Excel 97-2003 | `.xls` | `application/vnd.ms-excel` | every worksheet via xlrd | sheet, row |
| CSV / TSV | `.csv`, `.tsv` | `text/csv`, `text/tab-separated-values` | delimiter sniffing (`,` `;` tab `|`) | row |
| Google Sheets | native | `application/vnd.google-apps.spreadsheet` | exported as .xlsx (all sheets — CSV export would keep only the first) | sheet, row |
| PowerPoint | `.pptx` | `…presentationml.presentation` | per slide: title, text frames, grouped shapes, tables (→ records), speaker notes, pictures listed | slide, table, row |
| PowerPoint 97-2003 | `.ppt` | `application/vnd.ms-powerpoint` | **Unsupported** (reason: save as .pptx) | — |
| Google Slides | native | `application/vnd.google-apps.presentation` | exported as .pptx, parsed like PowerPoint | slide |
| Text / Markdown | `.txt`, `.md` | `text/plain`, `text/markdown` | blocks; Markdown headings and pipe tables (→ records) | block, section |
| RTF | `.rtf` | `application/rtf`, `text/rtf` | control words/groups stripped (objects, pictures, fields dropped) | block |
| HTML | `.html`, `.htm` | `text/html` | visible text, headings, tables (→ records), title/meta, JSON-LD (→ records); `script`/`style`/`iframe`/`object` ignored | block, table, json_ld |
| JSON | `.json` | `application/json` | list of objects or list under `properties`/`listings`/`items`/`data`/…; nested keys flattened; depth ≤ 64 | `json_path` |
| XML | `.xml` | `application/xml`, `text/xml` | defusedxml (DTD/entities refused); repeated element → records; attributes kept | `xml_path` |
| Images | `.jpg .jpeg .png .webp .tif .tiff .gif .bmp` | `image/*` | Pillow verify + pixel limit; OCR via Claude vision when enabled, otherwise `needs_ocr` | image (+ `ocr`) |
| ZIP | `.zip` | `application/zip` | in-memory; members ≤ 200, total ≤ 100 MB, ratio ≤ 100:1, no `..`/absolute paths, encrypted members refused, one nesting level | archive member + inner reference |
| Macro-enabled Office | `.docm .xlsm .pptm` | `…macroEnabled…` | **Unsupported** (macros never run) | — |
| Other Google types | Forms, Drawings, Sites, My Maps, Apps Script, Jamboard, shortcuts | `application/vnd.google-apps.*` | **Unsupported** with a reason | — |
| Executables / scripts | `.exe .dll .sh .bat .ps1 .js .jar …` or MZ/ELF/Mach-O/`#!` content | — | **Never opened** | — |

Every file gets a terminal status (`completed`, `partially_completed`, `failed`, `skipped`) and a reason when not
`completed`. Nothing is silently ignored. Encrypted/password-protected files are reported as unsupported (no
password workflow exists).

## 3. Statuses

* **File:** `pending → processing → completed | partially_completed | failed | skipped`
  (`failed` = retryable: transient Drive errors, corrupted/unparseable content, time limit; `skipped` = will not
  succeed on retry as-is: unsupported, encrypted, too large, download disabled, OCR needed but off, empty).
* **Batch:** `pending | processing | done` with per-status counts (`job.batches[]`).
* **Job:** `queued → processing → completed | partially_completed | failed | cancelled`.
* **Draft review:** `pending_review → approved | rejected | failed (save refused) | rolled_back`.
* **Proposed action:** `create | update | skip | manual_review` (+ optional admin `action_override`).

`manual_review` is set for: conflicting values between files, a non-THB price (needs decision), validation errors /
missing required fields (`property_name`, `property_type`, `province`, `price`, `status`), instruction-like text in the
source, or low confidence. `skip` is set for in-job duplicates (merged into the primary draft) and for listings that
already match with no field changes.

## 4. Unified property fields

Each draft carries every expected field (`null` when absent — never invented): property ID, name, project, country,
province, city, district, address, type, status, availability, price (min/max/display), currency, bedrooms
(min/max/display), bathrooms, area (sqm min/max/display + unit; sq ft converted and marked `inferred`), furnishing,
amenities, description, developer, ownership, pool/sea view/beach access, image URLs (from data) and embedded-image
references, plus source file name, Drive file ID, source reference (page/sheet/row/slide/path), per-field evidence
(quoted text or `column: value`), per-field status (`found` / `inferred` / `provided`), and confidence (field and draft).

Rules: values from structured columns and `Label: value` lines are taken as written; Claude extraction (when an
API key is configured) must quote evidence that **exists verbatim in the document and contains the value**,
otherwise the value is dropped (tested). Province/district/area may be inferred from known place names and are marked
`inferred` with the reason.

---

## 5. API reference (all under `/api/v1/admin/drive-ingestion`, admin or super admin)

Auth headers are the existing ones: `Authorization: Bearer <token>` and `X-Selected-Websites: <site>`.
The admin must have connected Google Workspace (`GET /api/v1/auth/google/login`).

```bash
export API=https://<backend-host>/api/v1
export TOKEN=<admin bearer token>
H=(-H "Authorization: Bearer $TOKEN" -H "X-Selected-Websites: tajlandia.com" -H "Content-Type: application/json")
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/formats` | Supported/unsupported formats, OCR flag, limits |
| POST | `/jobs` | Validate folder, discover files, start processing (202). Optional `Idempotency-Key` header |
| GET | `/jobs` | The caller's jobs |
| GET | `/jobs/{job_id}` | Job progress (`?include_files=true` for every file) |
| GET | `/jobs/{job_id}/files` | File list (`?status=failed` etc.) with reasons |
| GET | `/jobs/{job_id}/events` | Server-sent events: `progress` on change, `done` at the end |
| POST | `/jobs/{job_id}/retry` | Re-queue failed files only (`{"include_skipped_ocr": true}` after enabling OCR) |
| POST | `/jobs/{job_id}/resume` | Continue an interrupted job |
| POST | `/jobs/{job_id}/cancel` | Stop processing (drafts stay for review) |
| GET | `/jobs/{job_id}/drafts` | Review data (`?action=create|update|skip|manual_review`, `?review_status=`) |
| GET | `/jobs/{job_id}/drafts/{draft_id}` | One draft |
| PATCH | `/jobs/{job_id}/drafts/{draft_id}` | Correct values (`fields`), accept flagged values (`resolve_decisions`), set `action`, or `reject` |
| POST | `/jobs/{job_id}/approve` | **Writes.** `{"draft_ids": [...]} or {"all_ready": true}` **and** `"confirm": true` |
| POST | `/jobs/{job_id}/rollback` | Undo every saved draft of the job (`"confirm": true`) |

### cURL

```bash
curl "${H[@]}" "$API/admin/drive-ingestion/formats"

curl "${H[@]}" -X POST "$API/admin/drive-ingestion/jobs" -H "Idempotency-Key: import-2026-10-31-phuket" \
  -d '{"folder_url":"https://drive.google.com/drive/folders/<FOLDER_ID>","recursive":true,"batch_size":10}'

curl "${H[@]}" "$API/admin/drive-ingestion/jobs/<JOB_ID>"
curl "${H[@]}" "$API/admin/drive-ingestion/jobs/<JOB_ID>/files?status=failed"
curl -N "${H[@]}" "$API/admin/drive-ingestion/jobs/<JOB_ID>/events"
curl "${H[@]}" -X POST "$API/admin/drive-ingestion/jobs/<JOB_ID>/retry" -d '{}'
curl "${H[@]}" -X POST "$API/admin/drive-ingestion/jobs/<JOB_ID>/resume"
curl "${H[@]}" "$API/admin/drive-ingestion/jobs/<JOB_ID>/drafts?action=manual_review"
curl "${H[@]}" -X PATCH "$API/admin/drive-ingestion/jobs/<JOB_ID>/drafts/<DRAFT_ID>" \
  -d '{"fields":{"price_numeric_min":45500000},"action":"create"}'
curl "${H[@]}" -X POST "$API/admin/drive-ingestion/jobs/<JOB_ID>/approve" -d '{"all_ready":true,"confirm":true}'
curl "${H[@]}" -X POST "$API/admin/drive-ingestion/jobs/<JOB_ID>/rollback" -d '{"confirm":true}'
```

### Request / response examples (actual responses, recorded in `evidence/drive_ingestion_api_evidence.json`)

`POST /jobs` → **202**
```json
{"job_id": "dij_…", "folder_name": "Phuket Listings", "status": "partially_completed", "total_files": 21,
 "subfolders": 2,
 "counts_by_format": {"Excel (.xlsx)": 1, "Word (.docx)": 1, "Google Docs": 1, "Google Sheets": 1, "Google Slides": 1,
   "Word 97-2003 (.doc)": 1, "PDF": 6, "JSON": 1, "XML": 1, "Image": 1, "ZIP archive": 1, "CSV": 1, "HTML": 1,
   "Markdown (.md)": 1, "Unsupported": 1, "Text (.txt)": 1},
 "batch_size": 4, "batches_total": 6,
 "counts": {"pending": 0, "processing": 0, "completed": 12, "partially_completed": 0, "failed": 2, "skipped": 7},
 "progress_percent": 100.0,
 "draft_counts": {"total": 14, "by_action": {"manual_review": 3, "create": 8, "skip": 2, "update": 1}}}
```
(The status is already final because the evidence run processes inline; in production the response returns while
the job is `queued`/`processing`.) Repeating the call with the same `Idempotency-Key` returns the same `job_id` with
`"reused": true`.

`GET /jobs/{id}/files` (excerpt)
```text
completed | listings.xlsx        | Excel (.xlsx)       | batch 0 | 2 drafts
skipped   | old.doc              | Word 97-2003 (.doc) | Legacy Word 97-2003 (.doc) files cannot be parsed reliably. Save it as .docx …
skipped   | scan.pdf             | PDF                 | The PDF is scanned or image-only and has no text layer. OCR is not enabled …
skipped   | huge.pdf             | PDF                 | The file is 524,288,000 bytes, above the 26,214,400-byte limit.
skipped   | locked.pdf           | PDF                 | The owner has disabled downloading for this file.
skipped   | invoice.pdf          | PDF                 | The file content is an executable or script; it was not opened.
failed    | corrupted.pdf        | PDF                 | The PDF could not be opened (PdfStreamError); it may be corrupted.
failed    | flaky.csv            | CSV                 | Google Drive is temporarily unavailable (503) while downloading the file.
completed | notes.md             | Markdown (.md)      | No property data was recognised in this file.
skipped   | shortcut             | Unsupported         | Drive shortcuts are not followed; add the target file to the folder instead.
```

`GET /jobs/{id}/drafts` (one draft, trimmed)
```json
{"draft_id": "dd_…", "property_id": "lamai-garden-condo", "property_name": "Lamai Garden Condo",
 "effective_action": "create", "action_reasons": ["new listing"], "confidence": "high", "confidence_score": 0.95,
 "source": {"file_name": "listings.xlsx", "drive_file_id": "file_xlsx_01", "file_format": "xlsx",
            "reference": {"sheet": "Listings", "row": 3}, "label": "listings.xlsx · sheet Listings · row 3"},
 "fields": {"property_type": "Condominium", "status": "Under construction", "province": "Surat Thani",
            "district": "Lamai", "price_numeric_min": 8500000, "currency": "THB", "bedrooms_min": 1, "bedrooms_max": 2,
            "area_sqm_min": 45.0, "area_sqm_max": 68.0, "amenities": ["Pool", "Parking"], "furnishing": null, "…": "…"},
 "evidence": {"property_type": {"status": "found", "source": "column", "evidence": "Type: Condo",
                                "ref": {"sheet": "Listings", "row": 3}, "confidence": "high"}, "…": "…"},
 "missing_required": [], "validation_errors": {}, "conflicts": [], "duplicate_of": null, "existing_match": null}
```
Conflict example (Kamala Sky Villa in `listings.xlsx` row 2 vs `Kamala brochure.docx`): `effective_action:
"manual_review"`, `conflicts: [{"field": "price_numeric_min", "values": [{"value": 45000000, …}, {"value": 46000000, …}]}]`.

`POST /jobs/{id}/approve` without `confirm` → **400** `{"detail": "Approval needs explicit confirmation (confirm=true). Nothing was saved."}`

`POST /jobs/{id}/approve` `{"all_ready": true, "confirm": true}` → **200**
```json
{"job_id": "dij_…", "saved": 11, "failed": 0,
 "results": [{"draft_id": "dd_…", "saved": true, "operation_id": "crud_…", "property_id": "kamala-sky-villa",
              "action": "create", "approved_by": "admin@tajlandia.com",
              "message": "Saved: Kamala Sky Villa (kamala-sky-villa) - verified in database. … Undo operation id: crud_…", "sql": true, "firestore": true}, "…"]}
```
Repeating the approval returns `"already_approved": true` with the same `operation_id` (no second write).

`POST /jobs/{id}/rollback` `{"confirm": true}` → `{"undone": 11, "results": [{"operation_id": "crud_…", "undone": true, "message": "Operation 'crud_…' successfully undone. 1 properties restored to their previous state."}, …]}`

Errors: invalid link → **400**; folder missing or not shared → **403**; Google not connected → **409**; Firestore
not configured → **503**; unknown/foreign job → **404**; worker role → **403**.

---

## 6. Admin chatbot

| Admin says | Result |
|---|---|
| "Import all property data from this Google Drive folder. Process every supported file format, extract property details, identify duplicates and prepare records for my review: https://drive.google.com/drive/folders/…" | Folder validated; total files; counts by format; skipped-before-processing count; batch plan; OCR on/off; job started. Options: `batch size 5`, `top-level only` / `not recursive` |
| "import status" / "how is the import going" | Job + batch + file counts; failed/skipped/unsupported files with reasons |
| "show import drafts" / "review import" | Review table: property, action, confidence, source, issues; how many are ready |
| "retry failed import files" (add "OCR" to also retry OCR-skipped files) | Re-queues failed files only |
| "resume import" / "cancel import" | Resume or stop |
| "approve import" | Stages **one** `approve_drive_ingestion` pending action listing every listing to be written; **nothing is saved** |
| "confirm" / Confirm button | Existing confirm flow executes the approval; reply lists saved listings with undo operation ids |

The chat hook runs before the URL-import hook so a Drive folder link is never fetched as a public web page.

## 7. Configuration (`app/core/config.py`, all optional)

`DRIVE_INGEST_BATCH_SIZE`=10 · `DRIVE_INGEST_MAX_BATCH_SIZE`=25 · `DRIVE_INGEST_MAX_FILES`=1000 · `DRIVE_INGEST_MAX_DEPTH`=10 ·
`DRIVE_INGEST_RECURSIVE_DEFAULT`=true · `DRIVE_INGEST_MAX_FILE_BYTES`=25 MB · `DRIVE_INGEST_FILE_TIMEOUT_SEC`=180 ·
`DRIVE_INGEST_WORKERS`=2 · `DRIVE_INGEST_MAX_ATTEMPTS`=3 · `DRIVE_INGEST_MAX_ROWS`=5000 · `DRIVE_INGEST_MAX_PDF_PAGES`=300 ·
`DRIVE_INGEST_MAX_DRAFTS_PER_FILE`=500 · `DRIVE_INGEST_MAX_IMAGE_PIXELS`=60 M · `DRIVE_INGEST_ZIP_MAX_MEMBERS`=200 ·
`DRIVE_INGEST_ZIP_MAX_TOTAL_BYTES`=100 MB · `DRIVE_INGEST_ZIP_MAX_RATIO`=100 · `DRIVE_INGEST_OCR_ENABLED`=false ·
`DRIVE_INGEST_OCR_MODEL`="" (→ `ANTHROPIC_MODEL`) · `DRIVE_INGEST_OCR_MAX_PAGES`=10 · `DRIVE_INGEST_LLM_ENABLED`=true ·
`DRIVE_INGEST_LLM_MODEL`="" · `DRIVE_INGEST_LLM_MAX_CHARS`=40000 · `DRIVE_INGEST_LEASE_SEC`=300 ·
`DRIVE_INGEST_RESUME_ON_STARTUP`=true · `DRIVE_INGEST_ALLOW_MEMORY_STORE`=false.
New dependencies (in `requirements.txt`): `python-pptx`, `xlrd`, `defusedxml`.

## 8. Changes to existing files (additive only)

| File | Change |
|---|---|
| `app/services/action_service.py` | `save_ingested_listing(..., *, audit_tool_name="ingest_property_from_url")` keyword (default = old behaviour); stores `drive_ingestion` provenance in `raw_metadata.ingest`; confirm dispatch for `approve_drive_ingestion`; `IngestionError` mapped to 422 |
| `app/core/permissions.py` | `approve_drive_ingestion` requires `properties.import` (admin, super admin) |
| `app/services/admin_agent.py` | Drive-folder chat hook before the URL-import hook; confirmation reply for `approve_drive_ingestion` |
| `app/core/config.py` | `DRIVE_INGEST_*` settings; four of them shown in `/admin/system/config-status` |
| `app/main.py` | Router registration; startup resume of interrupted jobs (skipped under tests) |
| `requirements.txt` | `python-pptx`, `xlrd`, `defusedxml` |

---

## 9. Test cases and results

How the tests run: `pytest tests/test_drive_formats.py tests/test_drive_folder_ingestion.py`. Files are generated
in memory (reportlab, python-docx, openpyxl, python-pptx, Pillow, zipfile). Drive is a fake with pagination,
exports, download-disabled files, HTTP 404/503 errors and a write-call tripwire. Postgres is SQLite in memory;
Firestore is an in-memory fake. **The fixture fails the test if any statement reaches the configured database
engines**, and the evidence runs also blocked all external network and Firestore client creation. Anthropic is not
called (API key blanked).

Result legend: **PASSED** = executed in this session and passed · **NOT EXECUTED** = not run (needs a live environment).

### 9.1 Format-specific extraction
| ID | Case | Test | Result |
|---|---|---|---|
| F-01 | 29 name/MIME combinations classify to the right format/support flag; unsupported carry a reason | `test_classify_by_mime_and_extension` | PASSED |
| F-02 | Google Docs/Sheets/Slides export to docx/xlsx/pptx | `test_google_workspace_files_are_exported_to_office_formats` | PASSED |
| F-03 | Multi-page PDF keeps page numbers | `test_pdf_multi_page_text_keeps_page_numbers` | PASSED |
| F-04 | Mixed text/image PDF → `partial` with the image-only page listed | `test_pdf_with_some_image_only_pages_is_partial` | PASSED |
| F-05 | DOCX: headings, paragraphs (with section), tables → records, header, footer | `test_docx_paragraphs_headings_tables_header_footer` | PASSED |
| F-06 | Google Doc export keeps `google_doc` label | `test_google_doc_export_keeps_its_format_label` | PASSED |
| F-07 | XLSX: every sheet, header detection, blank rows skipped, sheet+row refs | `test_xlsx_every_sheet_with_header_detection_and_row_refs` | PASSED |
| F-08 | CSV with `;` delimiter | `test_csv_semicolon_delimited` | PASSED |
| F-09 | Google Sheet export label | `test_google_sheet_export_label` | PASSED |
| F-10 | PPTX: titles, text, tables (slide refs), notes, pictures | `test_pptx_slides_titles_tables_notes_and_pictures` | PASSED |
| F-11 | Google Slides export label | `test_google_slides_export_label` | PASSED |
| F-12 | TXT blocks, Markdown headings + tables, RTF stripped | `test_text_markdown_and_rtf` | PASSED |
| F-13 | HTML: scripts ignored, tables and JSON-LD kept | `test_html_ignores_scripts_keeps_tables_and_jsonld` | PASSED |
| F-14 | JSON list under `properties` with `json_path` refs; nested keys flattened | `test_json_list_under_known_key_with_paths` | PASSED |
| F-15 | XML repeated elements → records with `xml_path`; attributes kept | `test_xml_repeated_elements_become_records` | PASSED |
| F-16 | ZIP with mixed members; member refs; macOS junk ignored; unsupported member reported | `test_zip_mixed_members_with_member_refs` | PASSED |
| F-17 | Every format reaches the unified schema in one folder run (xlsx, docx, Google Doc/Sheet/Slides, JSON, XML, ZIP, HTML, PDF, TXT) | `test_every_format_reaches_the_unified_schema` | PASSED |
| F-18 | Real `.xls` workbook parsing | — (no `.xls` writer available to generate a fixture; only the corrupted case is automated) | NOT EXECUTED |

### 9.2 Google Drive authentication and permissions
| ID | Case | Test | Result |
|---|---|---|---|
| A-01 | Folder not found / not shared → 403 with guidance | `test_folder_access_errors` | PASSED |
| A-02 | Link to a file (not a folder) refused | `test_folder_access_errors` | PASSED |
| A-03 | Google Workspace not connected → 409 | `test_folder_access_errors` | PASSED |
| A-04 | Worker role refused (service + chat) | `test_roles_and_job_ownership`, `test_chat_refuses_workers` | PASSED |
| A-05 | Another admin cannot see a job; super admin can | `test_roles_and_job_ownership` | PASSED |
| A-06 | File with downloading disabled is skipped and never downloaded | `test_every_file_has_a_status_and_reason_none_silently_ignored` | PASSED |
| A-07 | Only read calls are made to Drive (write calls raise in the fake) | all job tests | PASSED |
| A-08 | Live OAuth token refresh and a real shared/Shared-Drive folder | — | NOT EXECUTED |

### 9.3 Pagination and nested folders
| ID | Case | Test | Result |
|---|---|---|---|
| P-01 | Folder listed page by page (`nextPageToken`) with no truncation | `test_discovery_pagination_recursion_counts_by_format` | PASSED |
| P-02 | Two levels of subfolders discovered; folder paths recorded | same | PASSED |
| P-03 | Non-recursive run lists skipped subfolders with a reason | `test_non_recursive_lists_skipped_subfolders` | PASSED |
| P-04 | Invalid links rejected (other host, no folder id, not a URL) | `test_invalid_folder_urls` | PASSED |

### 9.4 Mixed-format batches and job management
| ID | Case | Test | Result |
|---|---|---|---|
| B-01 | 21 files of 16 kinds in batches of 4 → 6 batches, all done, 100 % | `test_discovery_pagination_recursion_counts_by_format`, `test_every_file_has_a_status_and_reason_none_silently_ignored` | PASSED |
| B-02 | Every file has a status; every non-completed file has a reason | same | PASSED |
| B-03 | Batch size default 10 and limit 1–25 | `test_batch_size_limits` | PASSED |
| B-04 | One crashing parser does not stop the batch | `test_one_crashing_file_does_not_stop_the_batch` | PASSED |
| B-05 | SSE progress stream ends with `done` | `test_api_end_to_end` | PASSED |

### 9.5 Large, corrupted and malformed files
| ID | Case | Test | Result |
|---|---|---|---|
| L-01 | Oversized file (listed size) skipped before download | `test_every_file_has_a_status_and_reason_none_silently_ignored` | PASSED |
| L-02 | Size unknown → download stream stops at the limit | `test_download_size_cap_while_streaming` | PASSED |
| L-03 | Size limit before parsing | `test_size_limit_before_parsing` | PASSED |
| L-04 | Corrupted PDF / DOCX / XLS → error, not a crash | `test_encrypted_and_corrupted_pdfs`, `test_corrupted_docx_is_an_error_not_a_crash`, `test_xls_garbage_is_an_error` | PASSED |
| L-05 | Malformed / over-deep JSON | `test_json_malformed_and_too_deep` | PASSED |
| L-06 | Row limit → `partial` | `test_xlsx_row_limit_marks_partial` | PASSED |
| L-07 | Processing deadline stops long parsing | `test_deadline_stops_long_parsing` | PASSED |
| L-08 | Image pixel limit and corrupted image | `test_image_pixel_limit_and_corruption` | PASSED |

### 9.6 OCR and scanned documents
| ID | Case | Test | Result |
|---|---|---|---|
| O-01 | Scanned PDF with OCR off → `needs_ocr`, file skipped with reason | `test_scanned_pdf_needs_ocr_when_ocr_is_off` | PASSED |
| O-02 | Scanned PDF with OCR → text per page marked `ocr` | `test_scanned_pdf_is_read_with_ocr` (stub OCR) | PASSED |
| O-03 | OCR page budget → `partial` | `test_pdf_ocr_page_budget` | PASSED |
| O-04 | Image with OCR off → `needs_ocr`; with OCR → text | `test_image_needs_ocr_when_off_and_reads_text_with_ocr` (stub OCR) | PASSED |
| O-05 | Live Claude vision OCR on real scans | real-folder run §10.2 (36 scanned pages; 35 returned text) | PASSED (manual run) |

### 9.7 Duplicates, conflicts, normalisation, evidence
| ID | Case | Test | Result |
|---|---|---|---|
| D-01 | Same property in XLSX and DOCX → one primary, one `skip`; differing price → `manual_review` with both values and sources | `test_duplicates_across_formats_and_conflicts_need_manual_review` | PASSED |
| D-02 | Identical duplicate in a nested TXT merged without conflict | same | PASSED |
| D-03 | Existing Postgres listing → `update` with field changes | `test_existing_listing_is_an_update_with_changes` | PASSED |
| D-04 | Re-running a folder after approval proposes `skip`, not a second listing | `test_approve_writes_postgres_and_firestore_with_undo_audit_and_is_idempotent` | PASSED |
| D-05 | Normalised values, evidence strings, sheet/row/page refs, no invented furnishing | `test_drafts_are_normalised_with_evidence_and_source_refs` | PASSED |
| D-06 | Non-THB price and instruction-like text → `manual_review` | `test_non_thb_price_and_injection_text_go_to_manual_review` | PASSED |
| D-07 | LLM values whose quote is not in the document (or does not contain the value) are dropped | `test_llm_values_without_evidence_in_the_document_are_dropped` | PASSED |
| D-08 | Brochure layouts: label line followed by value line, bilingual/Polish labels, several pairs per line, id cut at the date | `test_brochure_label_on_one_line_value_on_next_and_bilingual_labels` | PASSED |
| D-09 | Placeholder prices (`8.xx million`, `XX`, `TBC`) never become numbers; flagged for a decision | `test_placeholder_price_is_never_a_number` | PASSED |
| D-10 | Floor-plan room dimensions are not bedroom counts | `test_floor_plan_room_dimensions_are_not_bedroom_counts` | PASSED |
| D-11 | Headings over marketing copy (`ADDRESS` / `FOR YOUR IDENTITY`) are not values | `test_brochure_headings_over_marketing_copy_are_not_values` | PASSED |
| D-12 | "BHK flats" map to Apartment (inferred) | `test_flats_map_to_apartment` | PASSED |

### 9.8 Retry, resume and idempotency
| ID | Case | Test | Result |
|---|---|---|---|
| R-01 | Retry downloads only failed files; transient failure then succeeds; completed files untouched | `test_retry_reprocesses_only_failed_files` | PASSED |
| R-02 | Retry limit (3 attempts) reported | same | PASSED |
| R-03 | Resume resets a file left `processing` and finishes the job | `test_resume_after_interruption` | PASSED |
| R-04 | Startup resume skips jobs under another instance's live lease, resumes after it lapses | `test_resume_interrupted_jobs_on_startup_respects_live_leases` | PASSED |
| R-05 | Same folder/options while active → same job; draft ids stable across reprocessing | `test_job_creation_is_idempotent_and_draft_ids_are_stable` | PASSED |
| R-06 | Approving twice → no second write (`already_approved`) | `test_approve_writes_postgres_and_firestore_with_undo_audit_and_is_idempotent` | PASSED |
| R-07 | Real process kill during a live job, then restart | — | NOT EXECUTED |

### 9.9 Security and malicious files
| ID | Case | Test | Result |
|---|---|---|---|
| S-01 | Executable disguised as PDF (MZ) never opened | `test_signatures_reject_executables_mismatches_and_encrypted_office`, job test | PASSED |
| S-02 | Encrypted Office file (OLE container) → `encrypted` | same | PASSED |
| S-03 | Encrypted PDF → `encrypted` | `test_encrypted_and_corrupted_pdfs` | PASSED |
| S-04 | Content/extension mismatch handled (PDF named .docx parsed as PDF; binary named .json refused) | `test_pdf_named_docx_is_parsed_as_what_it_really_is`, signature test | PASSED |
| S-05 | XML billion-laughs and XXE refused | `test_xml_entity_expansion_and_external_entities_are_refused` | PASSED |
| S-06 | ZIP bomb (ratio), total-size, member-count limits | `test_zip_bomb_ratio_total_size_and_member_count` | PASSED |
| S-07 | ZIP path traversal, encrypted member, nested-archive depth | `test_zip_path_traversal_nested_and_encrypted_members` | PASSED |
| S-08 | File names sanitised | `test_filenames_are_sanitised` | PASSED |
| S-09 | Macro-enabled Office and scripts never processed | `test_classify_by_mime_and_extension` | PASSED |
| S-10 | Logs contain no document text or tokens | `test_logs_never_contain_tokens_or_file_contents` | PASSED |
| S-11 | Instruction-like document text is not followed (draft flagged) | `test_non_thb_price_and_injection_text_go_to_manual_review` | PASSED |

### 9.10 Admin approval and database synchronisation
| ID | Case | Test | Result |
|---|---|---|---|
| W-01 | No Postgres or Firestore writes before approval | `test_no_database_writes_before_approval` | PASSED |
| W-02 | Approval without `confirm=true` refused | `test_approval_requires_explicit_confirmation`, API test | PASSED |
| W-03 | Approval writes Postgres **and** Firestore, provenance in `raw_metadata.ingest`, undo operation id per listing, audit entries (`ingest_property_from_drive_folder`, `drive_ingestion_approve`, `drive_ingestion_start`) | `test_approve_writes_postgres_and_firestore_with_undo_audit_and_is_idempotent` | PASSED |
| W-04 | Update of an existing listing changes only differing fields | same | PASSED |
| W-05 | `manual_review` cannot be approved until resolved; admin edit is recorded as `provided` | `test_manual_review_draft_can_be_resolved_then_approved` | PASSED |
| W-06 | Rejected drafts never saved | `test_rejected_and_incomplete_drafts_are_never_saved` | PASSED |
| W-07 | Rollback restores every listing | `test_rollback_undoes_every_saved_listing`, API test | PASSED |
| W-08 | Chat: folder → status → drafts → "approve import" stages one pending action → "confirm" saves 9 | `test_chat_full_flow_validate_progress_review_approve` | PASSED |
| W-09 | Full REST flow incl. retry, approve, 409 on editing an approved draft, rollback, 400/404 errors | `test_api_end_to_end` | PASSED |
| W-10 | Same against live Postgres + Firestore (staging) | — | NOT EXECUTED |

## 10. Execution evidence

| Run | Command | Result |
|---|---|---|
| New tests | `pytest -v tests/test_drive_formats.py tests/test_drive_folder_ingestion.py` (with the network/DB guard plugin) | **99 passed** — log: `evidence/drive_ingestion_pytest.txt` |
| Real folder | Public folder `1jhjvGFkpSYkYR2_Ti6USE6J0nD3snfJ3` (4 PDFs, 49 pages, 2 scanned) — draft-only, live Claude OCR + extraction | 4 files processed, 4 drafts, all in manual review with the missing fields listed — `evidence/drive_folder_1jhjv_review.md` / `.json` (see §10.2) |
| API walkthrough | 17 real HTTP calls through FastAPI `TestClient` against the simulated Drive | `evidence/drive_ingestion_api_evidence.json` (requests, status codes, responses, Postgres/Firestore rows after approval and after rollback) |
| Regression | full suite before vs after this change, external access blocked | see §10.1 |

### 10.1 Regression

The existing suite mostly talks to the database, Firestore and LLM APIs configured in `.env` (a remote Railway
Postgres and a real Firestore project). To avoid touching them, the **whole suite was run twice with a guard plugin
that blocks every external socket, the configured SQL engines and Firestore client creation**: once on an unchanged
checkout of `HEAD` (git worktree) and once on this change. Tests that need those services fail identically in both
runs, so any difference is caused by this change.

| Run | Passed | Failed | Errors | Skipped | Collected |
|---|---|---|---|---|---|
| Before (HEAD `9108fc0`) | 1310 | 137 | 173 | 112 | 1683 |
| After (this change) | 1415 | 137 | 173 | 112 | 1788 |

* The 310 failing/erroring test ids are **identical** in both runs (diffed by test id): **0 new failures, 0 regressions**.
  All 310 are tests that require the blocked remote database/Firestore/network.
* +105 tests at the time of the regression run: 94 new tests in the two new files (now 99), plus 11 existing permission-matrix cases
  (`test_chat_tool_permissions_and_drive_extraction.py`) that parametrize over every write tool and now also cover
  `approve_drive_ingestion` (workers denied, admins allowed) — all pass.
* Those 310 environment-dependent tests were **not** run against the real services in this session (doing so writes
  to the configured remote database). They should be run in CI/staging as usual.

### 10.2 Real Google Drive folder (2026-10-31)

Folder `https://drive.google.com/drive/folders/1jhjvGFkpSYkYR2_Ti6USE6J0nD3snfJ3` (shared "anyone with the link").
The files were downloaded without credentials (the admin OAuth path needs the deployed app) and run through the same
pipeline code; Postgres and Firestore were blocked and only `api.anthropic.com` was reachable.

| File | Result |
|---|---|
| Brouchar.pdf (24 scanned pages) | OCR 23/24 pages → draft **AYODHYAPURAM** (3 BHK apartments, developer Radhe, amenities) — location, price, status not in the readable pages |
| KALHAR RESIDENCY PRESENTATION 11.03.2026.pdf (5 pages, plans) | draft **KALHAR RESIDENCY** — type, location, price, status not stated |
| Suryoday Hills-3.pdf (12 scanned pages) | OCR 12/12 → draft **SURYODAY HILLS** (apartments, full Ahmedabad address, developer, amenities) — price, status not stated |
| Varsovia…Aileen_Residence_Bangtao….pdf (8 pages) | draft **Aileen Residence Bangtao** (villa, under construction, Bang Tao/Phuket, 2–3 bed, 3 bath, pool, ownership) — price is a template placeholder `8.xx million THB` |

Live Claude OCR (O-05) and live extraction were executed in this run. The real data exposed five parser gaps that
were fixed and covered by tests D-08…D-12: label/value on separate lines, Polish/bilingual labels, placeholder
prices (`8.xx` was previously parsed as 8 THB), floor-plan dimensions read as bedroom counts, and brochure headings
over marketing copy. Saving these drafts still needs the missing values and an admin approval in the deployed app.

## 11. Live-environment checklist (manual, NOT EXECUTED)

1. Staging: set `DRIVE_INGEST_OCR_ENABLED=true` only if sending page images to Anthropic is acceptable.
2. Connect Google Workspace for a staging admin; share a test folder containing one file of each format.
3. Run the chat prompt from §6; compare counts by format with the Drive UI.
4. Kill the server during processing, restart, confirm the job resumes (§9.8 R-07).
5. Review drafts, approve a subset, verify Postgres + Firestore + admin panel, then roll back.
6. Create a Firestore TTL policy on `drive_ingestion_*` if old jobs should expire (not configured by this change).

## 12. Known limitations

* Background processing uses in-process threads with a Firestore lease (no external queue exists in this
  codebase). A per-file time limit is enforced cooperatively inside parsers; a parser stuck inside a C library
  cannot be force-killed and is left to finish in the background while the file is marked failed.
* Embedded images are listed as references (name, page/slide) — their bytes are not uploaded to listing galleries.
  Image URLs present in structured data (e.g. an "Image URLs" column) are saved with new listings.
* Scanned documents and images need `DRIVE_INGEST_OCR_ENABLED=true` and an Anthropic API key.
* Unstructured documents yield one property per document from `Label: value` lines; multiple properties per document
  need the Claude extraction (API key) — every value it returns must be quoted from the document.
* Legacy `.doc` / `.ppt` and macro-enabled Office files are reported unsupported.
