# Scope compliance report - AI Chatbot backend

Date: 2026-10-28. Branch `dhruv-dev-backend-new-enhancement-bugs`. All changes are uncommitted for review.

**Method.** The source was exported (`git ls-files -co --exclude-standard`, no `.env`) and run against a throwaway
`pgvector/pgvector:pg16` PostgreSQL and the Firestore emulator (`gcloud emulators firestore`). No live database, Google
service, OpenAI or Anthropic call was used. Status words: **PASS** = verified by a passing test in that setup,
**FIXED** = a gap found in this audit and closed with tests, **GAP** = still open, **BLOCKED** = cannot be verified without
a live service or a decision.

## Test results (actual counts)

| Run | Passed | Failed | Skipped |
|---|---|---|---|
| Baseline, committed code, fresh DB (`tests`, minus the two live-Firestore files CI also ignores) | 1150 | 4 | 0 |
| After this work, fresh DB, same command (+257 new tests) | 1407 | 4 | 0 |

The 4 failures are identical before and after, all in `tests/test_admin_auth_and_audit_apis.py`
(`forgot_password`, `admin_register`, `activity_logs`, `data_history`). They log in with `settings.ADMIN_PASSWORD` and an
`admin@tajlandia.com` account that exists only in a deployed environment (HTTP 422, then 401 once a password and a seeded
user were supplied); CI deselects the same tests. They are **BLOCKED**, not passing. The two files
`test_firestore_auth.py` and `test_firestore_integration.py` need live Firestore credentials and were not run (as in CI).

Commands (from the isolated copy, with `DATABASE_URL`, `FIRESTORE_EMULATOR_HOST`, `ENABLE_FIRESTORE_SYNC=false`, dummy API keys):

    python -m pytest -q -rfEs tests --ignore=tests/test_firestore_auth.py --ignore=tests/test_firestore_integration.py
    python -m pytest -q -s tests/test_hallucination_eval.py        # prints the measured table

## Changes made

| File | Change |
|---|---|
| `app/core/permissions.py` | Added `properties.write / import / undo`, a `TOOL_PERMISSIONS` map and `tool_denial()`. Deny by default; non-string roles get nothing. |
| `app/services/admin_agent.py` | Chat tools that change property data are refused for roles without the permission (when proposed). Drive ingest refuses unreadable files and stages nothing. |
| `app/services/action_service.py` | Same check when an action is confirmed (a role can change after a proposal). Permanent delete refused unless `ALLOW_PERMANENT_DELETE`. |
| `app/core/config.py` | `ALLOW_PERMANENT_DELETE` (default `False`). |
| `app/services/drive_service.py` | Extraction status for every Drive file: ok / partial / needs_ocr / empty / error / not_found / too_large, and `usable`. Scanned PDFs report "OCR required". |
| `app/services/reply_validator.py` | Fixed money tokenizer (a shared `000` fragment made any comma-formatted price look supported). Checks more amenities and handover/delivery wording. |
| `app/services/admin_claim_verifier.py` | "I have updated / saved / emailed ..." now counts as a success claim that needs a real tool success. |
| `app/api/v1/superadmin_instructions.py` | `GET /admin/superadmin/conversations` (super admin): the list behind the Admin Panel's "AI Chat Logs" page (date, user + project, chat, AI response, model; search, role, model, website, date range, paging). |
| `app/services/chat_log_service.py`, `chat_log_query.py` | The log now also stores the staff member's name, the configured model, and a first-answer preview; `model` filter; "View" accepts the session id the list returns. |
| `tests/` | New: `test_chat_tool_permissions_and_drive_extraction.py` (105), `test_hallucination_eval.py` (3), `test_superadmin_conversation_list.py` (14). Updated: golden delete test (default refusal + flag-on behaviour), one test that staged a write with a role-less mock. |

No migration is needed: nothing changed in PostgreSQL or Firestore structure. Rollback = revert the files above
(`git checkout -- <file>`; delete the three new test files).

## Part 1 - Main scope

| Requirement | Status | Evidence / note |
|---|---|---|
| A. Public bot: search, details by id, memory, compare, cards, PDF, leads | PASS (model mocked) | Existing acceptance/honesty/brochure/lead tests. The language model itself was not called. |
| A. Never make things up; offer a human | PASS (dataset) | See hallucination section. |
| B. Admin answers from DB and documents | PASS (model mocked) | Existing admin agent tests. |
| B. Drive read + add/update listings, with preview and approval | PASS (local/mocked Drive); live Google BLOCKED | Staged as a pending action, applied only after yes. |
| B. PDF / Excel / Word reports | PASS | Existing report tests. |
| B. Email / calendar only after yes | PASS (mocked); live Gmail/Calendar BLOCKED | Existing validation and confirm tests. |
| B. Admin chat history reviewable | PASS (emulator) | `test_chat_log_*`. |
| C. Every change needs a clear yes; no silent bulk change | PASS | Propose/confirm tests, batch tests. |
| C. No permanent deletes | FIXED | REST `?hard_delete=true` used to delete permanently with a typed id. Now 403 unless `ALLOW_PERMANENT_DELETE`. Batch delete and REST delete archive. |
| C. Log every action | PASS | Audit end-to-end tests. Refused Drive imports are now audited as REFUSED. |
| C. Roles | FIXED (chat) / PASS (REST) | Workers could propose and confirm property changes through chat; now refused at both steps. |
| C. File/email/chat text is data | PASS | Drive wrapper escaping, URL-injection tests. |
| C. No keys in code; staging only | NOT AUDITED | Needs a secrets scan and deployment review. |

## Task areas

| Area | Status | Note |
|---|---|---|
| 1 Property CRUD per website, All, cross-site | PASS (REST, existing golden/site tests) | Chat writes use the same website scoping. "All websites" for a worker/admin is limited to their assigned sites. |
| 2 Drive: files, ingest, preview, failures | PASS (mock/local) | Added: missing, empty, corrupt, encrypted, scanned, oversize tests. Duplicate-import and retry behaviour not newly tested (a new proposal supersedes the open one). Sync beyond fetch/ingest is not implemented. |
| 3 Bulk upload, history, undo | PASS (emulator, existing tests) | Staged upload, worker refused, expired and second undo refused. A restart-recovery test was not added (snapshots are stored in Firestore, not memory). |
| 4 Logs, remarks, audit | PASS on Firestore | **Decision open:** the log and remarks live in Firestore, while the earlier plan said PostgreSQL. Not migrated; no Alembic revision exists for them. |
| 5 Role matrix | PARTIAL | Central matrix now covers chat logs and property changes (REST + chat). User management and the older routes still use the `require_*` guards (tested, not yet on the matrix). |
| 6 Link import | PASS | SSRF (private/loopback/metadata/redirect), size, type, scheme, injection, duplicates, worker refusal tests exist. Known limit: the address is checked, then the library connects by name (DNS rebinding window). |
| 7 Query logs and corrections | PASS (emulator) | Propose/approve/reject/revision/audit tests. |
| 8 Taught knowledge | PARTIAL | Super-admin instructions reach admin and public prompts (scoped by target, website, language). Approved corrections are applied by the **public** bot only (design: corrections come from visitor logs). |
| 9 Hallucination controls | PASS on dataset, see below | No claim about model-level rates. |
| 10 Scanned PDF / OCR | FIXED (detection) | Reports "OCR required"; OCR itself is not enabled (as decided). |
| 11 API consistency, retries, idempotency | GAP | Not standardized in this pass. |
| 12 Frontend contract | FIXED | See below. Frontend unit tests were not run. |

## Hallucination evaluation

**What it measures:** whether the backend claim checks flag replies whose claims the retrieved records do not support,
and leave faithful replies alone. It does **not** measure how often the language model writes an unsupported claim; that
needs live model calls.

**Dataset:** 8 faithful + 15 unsupported public replies (invented amenity, yield, property, ownership, date, wrong number)
against one verified listing; 4 faithful + 7 unsupported admin replies (wrong count, invented id/price, false success)
against a five-listing tool result. 41 hand-written cases, in `tests/test_hallucination_eval.py`.

| Checker | Before (HEAD) | After | Faithful replies wrongly flagged |
|---|---|---|---|
| Public `validate_reply` | 13/15 flagged | 15/15 | 0/8 |
| Admin `verify_admin_claims` | 2/7 flagged | 3/7 | 0/4 |

Fixed along the way: a price such as "9,900,000 THB" was accepted against a 12.5M listing.

**Known admin gaps (pinned in the test, not hidden):** an invented price or id is only logged as a "soft" mismatch by
design, and a count claim passes when the same number appears elsewhere in the data ("12 properties" vs a 12,000,000
price). The figures describe this small dataset only.

## Property CRUD by role and selected website (`tests/test_property_crud_by_selected_website.py`, 48 tests)

Real accounts and tokens (super admin, tajlandia-only admin, tajlandia+varsovia admin, worker); the selected website is sent in
`X-Selected-Websites` as the panel does. REST forms and the chat flow (propose with the admin-chat tool, confirm with
`POST /admin/actions/{id}/confirm`) are both covered. On the committed code 9 of these tests failed; all 48 pass now.

Bugs found and fixed:
* **Chat created a listing on the environment's default website**, not the one(s) the admin selected (it looked right only
  when the two matched). It now lands on every selected website, like the REST form.
* **Chat could create a listing on a website outside the account** (an explicit `websites` value was not checked), or on a
  website not selected. Refused at proposal and again at confirm.
* **Chat confirm did not re-check scope**: after an account lost a website, an old proposal still wrote to it. The confirm now
  uses the confirming account's websites, and refuses a listing the account cannot reach (Postgres or Firestore-only).
* **Duplicates**: chat could create a second listing with an existing name, and neither chat nor REST update blocked a rename
  into another listing's name. Both now refuse (409 / `DUPLICATE`); creating with an existing id updates the same row.
* Permanent delete via REST was open (fixed earlier, now covered by a role test).

Verified working: create/read/update/delete per selected website, one and two websites, super admin on any site, worker
read-only, no token 401, validation 422 stores nothing, delete = archive and repeatable, cross-website get/update/delete
refused, propose waits for a yes, reject writes nothing, confirm twice writes once, only the proposer can confirm.

Not covered: your staging Firestore (no credentials were available here), Firestore-backed listings (Firestore off in these tests; the Firestore-only scope check is code-reviewed, not
tested here), and the language model choosing the tool (the tests call the tool the model would call).

## Google Workspace and Drive (`tests/test_google_workspace_per_account.py`, 19 tests; `tests/test_admin_workspace_status_chat.py`, 28)

Google is faked (no network), so these show which credentials the backend hands to Google, not that a real consent works.
* **Fixed:** a staff member who had not connected Google (or whose token refresh failed) silently used the shared
  `google_token.json` / `service_account.json`, i.e. someone else's Drive. Each staff member now acts only through their own
  account; the shared one needs `GOOGLE_SHARED_CREDENTIAL_FALLBACK=true`.
* Verified: super admin, admin and worker each read Drive with their own token; accounts are never mixed; an unconnected
  worker never reaches Drive; reading makes no Drive write call; the only Drive permission requested is `drive.readonly`
  (the backend cannot write to Drive; listings go to the database after a confirm).
* **Fixed:** asking the assistant "my drive is connected ?" returned the off-topic refusal. Drive/Gmail/Calendar/Workspace
  status questions are now answered from the account (connected email, services actually granted, or how to connect) and
  Google wording is no longer refused as off-topic.
* **Not verified:** your real Drive and the Varsovia PDF. If a PDF is image-only the assistant now says "OCR required" instead
  of reading nothing; I could not open your file to confirm which case it is.

## Bulk upload, history, undo (`tests/test_bulk_upload_e2e.py`, 28 tests, Firestore emulator)

Real files (CSV, Excel, Word, PDF, scanned PDF), real roles, the endpoints the modal calls.
Bugs found and fixed:
* Website scope never applied (the endpoint did not resolve the selected websites): an admin could update or delete another
  website's listings with a file. Now scoped to the selection, kept in the staged action and re-checked at confirm.
* **Create could overwrite an existing listing with the same id**, and could duplicate a name or an id repeated in the file.
  Now skipped and reported in `skipped[]`.
* Create dropped columns not in the preview (a `price` column was lost, then validation failed with HTTP 500). Prices are now
  read as numbers, a non-positive price is rejected, and row errors are returned at upload (400), not at confirm (was 500).
* History and undo were open to every admin (history to workers too) and showed operations of other websites. Now admin and
  super admin only, filtered by website; an operation you may not see is 404.
* Scanned PDFs give an "OCR required" message.

Verified: nothing is written before the confirm; each file type stages and applies; delete is an archive and undo restores it;
undo works once and from the stored snapshot alone; expired operations are refused and hidden; only the uploader confirms; a
worker cannot upload, undo or see history.
**Open gap:** bulk operations write Firestore only, not PostgreSQL (REST CRUD writes both). Mixed-store listings can differ.
The modal text "Execute Instantly" and "Permanently removes" does not match the backend (it stages first; delete archives).

## Activity logs: data sent once (`tests/test_audit_log_single_copy.py`, 11 tests)

`/admin/activity-logs` and `/admin/data-history` repeated the same data (`fields` and `fields_to_update`, the preview table
again under `change_diff`, the payload again inside the result) and listed an action written to both stores twice. Each piece
is now sent once, where it first appears; short values ("500 THB") are kept; empty sections are dropped; the result stays a
non-empty text; the same action from PostgreSQL and Firestore is one row.

## Chat Logs page (Admin Panel design)

| Design element | Backend |
|---|---|
| Date & time, User & project (name, email, website chip) | `created_at` (last message), `user_name`, `user_email`, `role`, `project` |
| Chat / AI Response columns | `user_message` (first question), `ai_response` (first answer preview; older rows read it from the stored messages) |
| Model badge, Model filter | `model` = the model configured for that bot (the configured name, not proof of which fallback answered); partial-match filter |
| Search, role filter, date range, chat-bot selector | `search`, `role`, `start_date`/`end_date`, `website` |
| View -> | `GET /admin/superadmin/conversations/{session id}` returns the messages |
| Pager | `limit` / `offset` / `total` |

Limits: visitors have no name or email (the panel should show "Visitor"); rows logged before this change have no model.
The panel only accepts UUID ids; both frontends generate UUID session ids, so every real row is listed.

## Frontend contract check

Every `/api/v1/...` path in `ai-chatbot-web-admin` (48) and `ai-chatbot-web-standalone` (8) was matched against the
backend's OpenAPI paths. One real mismatch: the admin panel lists chat logs at `GET /admin/superadmin/conversations`, which
the backend did not serve (the new list is `/admin/chat-logs/conversations`). Added a compatible endpoint. The other
unmatched path was a URL prefix constant, not a call. Response shapes were compared by reading the panel's parser; the
panel was not run.

## Decisions needed

1. Keep the chat log and remarks in Firestore (working, tested), or move them to PostgreSQL as first planned.
2. Should approved corrections also apply in the admin chat?
3. Should an invented price or id in an admin reply be a hard stop? It would block some good answers (derived values, ids the user typed).
4. Email and calendar tools are open to workers (they only draft, and need a yes). Restrict them?
