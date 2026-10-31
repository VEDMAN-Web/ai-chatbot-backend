# API endpoints by feature

Generated from the backend's OpenAPI schema (28 Oct 2026). Base URL = the backend host (local: `http://localhost:8000`).
Every path below starts with `/api/v1`. Send `Authorization: Bearer <token>` on every staff route. On website-scoped
routes send `X-Selected-Websites: tajlandia.com,varsoviaestate.com` (the panel's selected chat-bot/website boxes); without it the
account's full website list is used. Roles: **SA** super admin, **A** admin, **W** worker. Interactive docs: `/docs`.

## 1. Sign-in and account
| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/auth/login` | anyone | `{email, password}` -> `access_token`, role, allowed websites |
| POST | `/auth/logout` | staff | ends the session |
| GET | `/auth/me` | staff | role, allowed websites, Google status |
| POST | `/auth/forgot-password`, `/auth/reset-password`, `/auth/change-password` | anyone / staff | |
| POST | `/auth/register` | per policy | |

## 2. Google Workspace (each staff member's own account; Drive is read-only)
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/auth/google/login` | staff | returns `auth_url` (or `?redirect_mode=true` redirects) |
| GET | `/auth/google/callback` | Google redirect | stores tokens on the signed-in staff member only |
| GET | `/auth/google/status` | staff | connected? scopes |
| POST | `/auth/google/disconnect` | staff | |

Needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` (the repo's `.env` still has the placeholder client id).

## 3. Property CRUD (Admin Panel forms)
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/admin/properties` | SA A W | filters: location, property_type, min/max_price, bedrooms, has_pool, status, limit(<=200), offset; only the selected websites |
| GET | `/admin/properties/{property_id}` | SA A W | 403 if the listing is on another website |
| POST | `/admin/properties` | SA A | 201; 409 duplicate id or name; `websites` must be selected + allowed; `?allow_without_undo=true` if undo storage is unavailable |
| PUT | `/admin/properties/{property_id}` | SA A | partial update; 409 on a name that belongs to another listing |
| DELETE | `/admin/properties/{property_id}` | SA A | archives (soft delete). `?hard_delete=true` is refused (403) unless `ALLOW_PERMANENT_DELETE` is on |
| GET | `/admin/properties/{property_id}/consistency` | SA A | Postgres vs Firestore check |

## 4. Admin chatbot and confirmations
| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/admin/chat` | SA A W | `{message, session_id, selected_websites?, language?}`; `?stream=true` for SSE |
| GET | `/admin/chat/history/{session_id}` | SA A W | your own session; admins and super admins can open any |
| GET | `/admin/actions/{pending_action_id}` | SA A W | status of a staged change |
| POST | `/admin/actions/{pending_action_id}/confirm` | proposer only | applies the staged change; workers cannot confirm property changes |
| POST | `/admin/actions/{pending_action_id}/reject` | proposer only | |

## 5. Bulk upload, history, 6-day undo (the "Bulk Property Operations" modal)
| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/admin/properties/batch-file` | SA A | multipart: `file` (.xlsx .xls .csv .pdf .docx, <=5 MB, <=500 rows), `action` = `update` / `delete` / `create`. **202** = staged with a per-row preview and `pending_action_id`. Nothing is written yet. 400 for bad file/rows/scanned PDF |
| POST | `/admin/actions/{pending_action_id}/confirm` | uploader | applies the previewed rows; response has `execution_result.operation_id`, `skipped[]` |
| GET | `/admin/operations/history?limit=50` | SA A | operations the caller may see (their websites); expired ones hidden |
| POST | `/admin/operations/{operation_id}/undo` | SA A | within 6 days, once; 404 if unknown or not yours |
| POST | `/admin/maintenance/purge-expired` | SA A | removes expired snapshots |

Panel text to correct: the modal says "Execute Instantly" and "Permanently removes". The backend stages first (the panel must
call confirm) and delete is an archive that undo can reverse.

## 6. Google Drive in the admin chat
No separate REST route: ask the admin chat ("read file X from Drive", "add the listing from this Drive file"). The chat tools are
`fetch_drive_file` (any staff, own Google account) and `ingest_property_from_drive` (SA A; staged, needs a confirm).

## 7. Link import (paste a property link)
Also through `/admin/chat` (tool `ingest_property_from_url`, SA A): shows a preview, the admin edits/confirms in chat.

## 8. Logs and audit
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/admin/activity-logs` | SA A W | audit trail (who/what/when) |
| GET | `/admin/data-history` | SA A W | before/after of data changes |

## 9. AI chat logs, remarks, corrections
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/admin/superadmin/conversations` | SA | the "AI Chat Logs" list: `search, role, model, website, start_date, end_date, limit, offset` -> `{total, limit, offset, logs[]}`; each row has `conversation_id` (session id), `user_name`, `user_email`, `role`, `project`, `model`, `created_at`, `user_message`, `ai_response` |
| GET | `/admin/superadmin/conversations/{conversation_id}` | SA | "View": messages, pending actions, audit rows |
| GET | `/admin/chat-logs/conversations` | SA A W | richer list (filters, sort, page/page_size); each role sees only what it may |
| GET | `/admin/chat-logs/conversations/{conversation_id}` | SA A W | one conversation with evidence/sources |
| POST | `/admin/chat-logs/conversations/{conversation_id}/messages/{message_id}/remarks` | SA A | add a remark / suggested answer |
| GET/PATCH | `/admin/chat-logs/remarks`, `/remarks/{id}` | SA A | list, read, edit; `/remarks/{id}/revisions` |
| POST | `/admin/chat-logs/remarks/{id}/correction` | SA A | propose a corrected answer |
| GET/PATCH | `/admin/chat-logs/corrections`, `/corrections/{id}` | SA A | list, read, edit; `/corrections/{id}/revisions` |
| POST | `/admin/chat-logs/corrections/{id}/approve` `/reject` `/deactivate` | SA | only approved corrections change what the user bot answers |

## 10. Admin-taught instructions (super admin)
| Method | Path | Notes |
|---|---|---|
| POST/GET | `/admin/superadmin/instructions` | create / list |
| GET/PATCH | `/admin/superadmin/instructions/{id}` | read / edit |
| POST | `/admin/superadmin/instructions/{id}/activate` `/archive` `/rollback` | lifecycle |
| GET/POST | `/admin/superadmin/instructions/export`, `/import` | |
| POST/GET | `/admin/superadmin/remarks`, `/remarks/{id}/convert`, `/remarks/{id}/dismiss` | turn a remark into an instruction |

## 11. User management and roles
| Method | Path | Who | Notes |
|---|---|---|---|
| GET/POST | `/auth/admin/users` (alias `/admin/users`) | SA A | an admin can manage workers only; SA manages everyone |
| GET/PUT/PATCH/DELETE | `/auth/admin/users/{user_id}` | SA A | |
| POST | `/auth/admin/users/{user_id}/block` | SA | blocks and ends sessions |

## 12. Reports and brochures
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/admin/reports/{filename}` | staff | download a generated PDF/Excel/Word report |
| POST | `/admin/reports/{filename}/link` | staff | short-lived signed link |
| GET | `/admin/reports/download/{token}` | signed link | |
| GET | `/admin/brochure/preview` | staff | |

## 13. Public (user) chatbot - no login
| Method | Path | Notes |
|---|---|---|
| POST | `/chat/public` | `{message, session_id, site_origin?, language?}` |
| GET | `/chat/history/{session_id}` | |
| POST | `/chat/lead` | save a lead |
| GET | `/chat/brochure/{property_id}` | property PDF |
| GET | `/chat/contact-info` | |
| GET | `/properties`, `/properties/{property_id}` | public listing data |
| GET | `/branding`, `/health` | |

## 14. System
| Method | Path | Who |
|---|---|---|
| GET | `/admin/system/config-status` | SA A |
