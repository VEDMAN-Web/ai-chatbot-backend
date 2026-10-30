# SPARK BETA TECH AI CHATBOT PLATFORM - COMPLETE API REFERENCE
**Version:** 1.0.0 | **Framework:** FastAPI / Python 3.10 | **Target:** Production & Staging
**Websites Powered:** `Tajlandia.com` | `VarsoviaEstate.com` | `Thai.estate` | Admin Control Panel

---

## TABLE OF CONTENTS

1. [Architecture & Protocol Standards](#1-architecture--protocol-standards)
2. [Global Authentication & Role-Based Access Control](#2-global-authentication--role-based-access-control)
3. [Multi-Tenant Tenant Domain Scoping](#3-multi-tenant-domain-scoping)
4. [Master Endpoints Inventory (93 Operations)](#4-master-endpoints-inventory)
5. [Public Customer Chatbot & Leads](#5-public-customer-chatbot-and-leads)
6. [Property Catalog & Public Search](#6-property-catalog-and-public-search)
7. [Authentication & Identity Management](#7-authentication-and-identity-management)
8. [Admin Staff & User Directory](#8-admin-staff-and-user-directory)
9. [Internal Staff Chatbot Agent](#9-internal-staff-chatbot-agent)
10. [Two-Step Staged Action Gate (HITL Governance)](#10-two-step-staged-action-gate-hitl-governance)
11. [Document & Report Generation (PDF, XLSX, DOCX)](#11-document-and-report-generation-pdf-xlsx-docx)
12. [Query Logs, Analytics & Answer Corrections (N3, N4, N5)](#12-query-logs-analytics-and-answer-corrections-n3-n4-n5)
13. [Superadmin Business Instructions & Knowledge Teaching (N7)](#13-superadmin-business-instructions-and-knowledge-teaching-n7)
14. [Property Inventory Management & Batch Undo (C1, C2)](#14-property-inventory-management-and-batch-undo-c1-c2)
15. [System Diagnostics, Audit Logs & Maintenance (C3, C4)](#15-system-diagnostics-audit-logs-and-maintenance-c3-c4)
16. [Standard Error Responses & Status Codes](#16-standard-error-responses--status-codes)

---

## 1. ARCHITECTURE & PROTOCOL STANDARDS

All endpoints conform to standard REST architectural patterns over HTTP/HTTPS with JSON message bodies, with streaming responses supported for document binaries (PDF, XLSX, DOCX).

### Base URLs
- **Staging Environment (Local):** `http://127.0.0.1:8000`
- **Staging Cloud (Railway):** `https://crossover.proxy.rlwy.net:55955`
- **Production Cloud (Cloud Run):** Configured via domain ingress `https://api.tajlandia.com`

### Standard Headers
```http
Content-Type: application/json
Authorization: Bearer <jwt_access_token>   # Required for all staff/admin endpoints
x-site-origin: tajlandia.com              # Optional tenant routing override
x-selected-websites: tajlandia.com         # Multi-site catalog scoping
```

---

## 2. GLOBAL AUTHENTICATION & ROLE-BASED ACCESS CONTROL

Authentication is enforced via stateless JSON Web Tokens (JWT) signed with HMAC-SHA256 (`HS256`). Tokens carry user identity, assigned role, and allowed website domains.

| Role Level | Hierarchy | Permissions & Scope |
|:---|:---:|:---|
| **Public Visitor** (`anonymous`) | Level 0 | Unauthenticated access to public chat (`/api/v1/chat/public`), property listings (`/api/v1/properties`), branding, and PDF brochure downloads. |
| **Staff Worker** (`worker`) | Level 1 | Access to internal staff chatbot (`/api/v1/admin/chat`), read-only property history, document generation, and proposing changes. Blocked from administrative deletions or purge maintenance. |
| **Super Admin** (`super_admin`) | Level 2 | Complete operational access: approving staged write actions, executing rollback/undo, modifying staff users, instruction teaching, and system maintenance. |

---

## 3. MULTI-TENANT DOMAIN SCOPING

The platform operates as a unified multi-tenant engine serving three core real estate brands from a shared codebase:
1. **`tajlandia.com`** (and `tajlandia.pl`): Primary Thailand investment portal for European / Polish / International buyers.
2. **`thai.estate`**: English-first luxury villa and condominium brokerage targeting global expatriates and investors.
3. **`varsoviaestate.com`**: European / Warsaw residential and commercial properties.

Tenant identification resolves dynamically via `site_origin` in request bodies, query parameters, or `x-site-origin` HTTP headers.

---

## 4. MASTER ENDPOINTS INVENTORY (93 OPERATIONS)

| Method | Endpoint Path | Category | Auth | Roles | Description |
|:---:|:---|:---|:---:|:---:|:---|
| `GET` | `/api/v1/chat/brochure/file/{filename}` | Public | No | `public` | Get Local Stored Brochure File |
| `GET` | `/api/v1/chat/brochure/shortlist` | Public | No | `public` | Generates and downloads a professional PDF comparing and sho |
| `GET` | `/api/v1/chat/brochure/{property_id}` | Public | No | `public` | Generates and downloads a professional property brochure PDF |
| `GET` | `/api/v1/chat/contact-info` | Public | No | `public` | Returns the public phone / WhatsApp numbers of the site and  |
| `GET` | `/api/v1/chat/history/{session_id}` | Public | No | `public` | Restores conversation history and saved preferences for a se |
| `POST` | `/api/v1/chat/lead` | Public | No | `public` | Captures customer contact details directly from modal or cal |
| `POST` | `/api/v1/chat/public` | Public | No | `public` | Unified conversational & lead capture endpoint for visitors  |
| `GET` | `/api/v1/branding` | Property | No | `public` | Returns active tenant branding, bot name, and contact detail |
| `GET` | `/api/v1/properties` | Property | No | `public` | Direct property filtering endpoint using safe SQLAlchemy que |
| `GET` | `/api/v1/properties/{property_id}` | Property | No | `public` | Lookup property by exact Property ID with optional website s |
| `GET` | `/health` | Property | No | `public` | System health check, PostgreSQL (admin and user instances),  |
| `POST` | `/api/admin/auth/login` | Authentication | No | `public` | Authenticates an administrative / staff user with email and  |
| `POST` | `/api/admin/auth/register` | Authentication | No | `public` | Multi-user registration endpoint. |
| `POST` | `/api/v1/auth/change-password` | Authentication | Yes | `worker, super_admin` | Changes the authenticated administrator's password. |
| `POST` | `/api/v1/auth/forgot-password` | Authentication | No | `public` | Generates a secure password reset token for an administrator |
| `GET` | `/api/v1/auth/google/callback` | Authentication | No | `public` | Receives authorization code from Google, exchanges it for ac |
| `POST` | `/api/v1/auth/google/disconnect` | Authentication | No | `public` | Revokes and unlinks the connected Google account for the aut |
| `GET` | `/api/v1/auth/google/login` | Authentication | No | `public` | Generates the Google OAuth2 authorization URL for the authen |
| `GET` | `/api/v1/auth/google/status` | Authentication | No | `public` | Returns the Google Workspace connection status for the authe |
| `POST` | `/api/v1/auth/login` | Authentication | No | `public` | Authenticates an administrative / staff user with email and  |
| `POST` | `/api/v1/auth/logout` | Authentication | No | `public` | Terminates the active user session and revokes the token. |
| `GET` | `/api/v1/auth/me` | Authentication | Yes | `worker, super_admin` | Returns the authenticated user's profile based on the active |
| `POST` | `/api/v1/auth/register` | Authentication | No | `public` | Multi-user registration endpoint. |
| `POST` | `/api/v1/auth/reset-password` | Authentication | No | `public` | Resets the administrator password using a valid cryptographi |
| `GET` | `/api/v1/admin/users` | Admin | Yes | `worker, super_admin` | Directory endpoint for admins and super admins to inspect st |
| `POST` | `/api/v1/admin/users` | Admin | Yes | `worker, super_admin` | Creates a new administrator or worker user account. |
| `DELETE` | `/api/v1/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Deletes a user account and cascades active sessions. |
| `GET` | `/api/v1/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Retrieves details and status for a specific user account. |
| `PATCH` | `/api/v1/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Updates user details (name, password reset, role, active sta |
| `PUT` | `/api/v1/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Updates user details (name, password reset, role, active sta |
| `GET` | `/api/v1/auth/admin/users` | Admin | Yes | `worker, super_admin` | Directory endpoint for admins and super admins to inspect st |
| `POST` | `/api/v1/auth/admin/users` | Admin | Yes | `worker, super_admin` | Creates a new administrator or worker user account. |
| `DELETE` | `/api/v1/auth/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Deletes a user account and cascades active sessions. |
| `GET` | `/api/v1/auth/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Retrieves details and status for a specific user account. |
| `PATCH` | `/api/v1/auth/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Updates user details (name, password reset, role, active sta |
| `PUT` | `/api/v1/auth/admin/users/{user_id}` | Admin | Yes | `worker, super_admin` | Updates user details (name, password reset, role, active sta |
| `POST` | `/api/v1/auth/admin/users/{user_id}/block` | Admin | Yes | `worker, super_admin` | Super Admin API to block or unblock any administrator or wor |
| `POST` | `/api/v1/admin/chat` | Internal | Yes | `worker, super_admin` | Authenticated Admin / Internal Team AI Agent unified endpoin |
| `GET` | `/api/v1/admin/chat/history/{session_id}` | Internal | Yes | `worker, super_admin` | Retrieves full admin multi-turn conversation history from Fi |
| `GET` | `/api/v1/admin/actions/{pending_action_id}` | Two-Step | Yes | `worker, super_admin` | Retrieves current status and payload details of a staged pen |
| `POST` | `/api/v1/admin/actions/{pending_action_id}/confirm` | Two-Step | Yes | `worker, super_admin` | Explicit Two-Step Confirmation endpoint for write actions. |
| `POST` | `/api/v1/admin/actions/{pending_action_id}/reject` | Two-Step | Yes | `worker, super_admin` | Rejects a staged pending action and logs the rejection in au |
| `GET` | `/api/v1/admin/brochure/preview` | Document | Yes | `worker, super_admin` | Admin-only design preview: return a single-property brochure |
| `GET` | `/api/v1/admin/reports/download/{token}` | Document | Yes | `worker, super_admin` | Public (token-gated) download — no Bearer header required. |
| `GET` | `/api/v1/admin/reports/{filename}` | Document | Yes | `worker, super_admin` | Downloads a generated PDF/XLSX/DOCX/CSV property intelligenc |
| `POST` | `/api/v1/admin/reports/{filename}/link` | Document | Yes | `worker, super_admin` | Return a short-lived HMAC signed download URL (valid ~10 min |
| `GET` | `/api/v1/admin/chat-logs/conversations` | Query | Yes | `worker, super_admin` | Conversations the caller may see, filtered, searched, sorted |
| `GET` | `/api/v1/admin/chat-logs/conversations/{conversation_id}` | Query | Yes | `worker, super_admin` | One conversation with its messages in chronological order. |
| `POST` | `/api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_id}/remarks` | Query | Yes | `worker, super_admin` | An internal remark on one bot response. It never changes wha |
| `PATCH` | `/api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_seq}/feedback` | Query | Yes | `worker, super_admin` | Adds user feedback (thumbs_up/thumbs_down) to a logged assis |
| `GET` | `/api/v1/admin/chat-logs/corrections` | Query | Yes | `worker, super_admin` | List Corrections |
| `GET` | `/api/v1/admin/chat-logs/corrections/{correction_id}` | Query | Yes | `worker, super_admin` | Get Correction |
| `PATCH` | `/api/v1/admin/chat-logs/corrections/{correction_id}` | Query | Yes | `worker, super_admin` | Editing returns the correction to 'proposed': an edited answ |
| `POST` | `/api/v1/admin/chat-logs/corrections/{correction_id}/approve` | Query | Yes | `worker, super_admin` | Approve Correction |
| `POST` | `/api/v1/admin/chat-logs/corrections/{correction_id}/deactivate` | Query | Yes | `worker, super_admin` | Deactivate Correction |
| `POST` | `/api/v1/admin/chat-logs/corrections/{correction_id}/reject` | Query | Yes | `worker, super_admin` | Reject Correction |
| `GET` | `/api/v1/admin/chat-logs/corrections/{correction_id}/revisions` | Query | Yes | `worker, super_admin` | Correction Revisions |
| `GET` | `/api/v1/admin/chat-logs/remarks` | Query | Yes | `worker, super_admin` | List Remarks |
| `GET` | `/api/v1/admin/chat-logs/remarks/{remark_id}` | Query | Yes | `worker, super_admin` | Get Remark |
| `PATCH` | `/api/v1/admin/chat-logs/remarks/{remark_id}` | Query | Yes | `worker, super_admin` | The author (or a reviewer) edits the text; only a reviewer c |
| `POST` | `/api/v1/admin/chat-logs/remarks/{remark_id}/correction` | Query | Yes | `worker, super_admin` | Propose a corrected answer. It is only a proposal until a su |
| `GET` | `/api/v1/admin/chat-logs/remarks/{remark_id}/revisions` | Query | Yes | `worker, super_admin` | Remark Revisions |
| `POST` | `/api/v1/admin/corrections` | Query | Yes | `worker, super_admin` | Create a new answer correction directly (without going throu |
| `GET` | `/api/v1/admin/superadmin/conversations` | Superadmin | Yes | `super_admin` | The list the Admin Panel's chat-log page loads. It reads the |
| `GET` | `/api/v1/admin/superadmin/conversations/{conversation_id}` | Superadmin | Yes | `super_admin` | Read-only chat, pending actions and audit rows. Nothing is w |
| `GET` | `/api/v1/admin/superadmin/conversations/{conversation_id}/review` | Superadmin | Yes | `super_admin` | What the 'Review AI Response' dialog shows: the full questio |
| `POST` | `/api/v1/admin/superadmin/conversations/{conversation_id}/review` | Superadmin | Yes | `super_admin` | Save Review. Either tick 'satisfactory', or give instruction |
| `GET` | `/api/v1/admin/superadmin/instructions` | Superadmin | Yes | `super_admin` | List Instructions |
| `POST` | `/api/v1/admin/superadmin/instructions` | Superadmin | Yes | `super_admin` | Create Instruction |
| `GET` | `/api/v1/admin/superadmin/instructions/export` | Superadmin | Yes | `super_admin` | Export Instructions |
| `POST` | `/api/v1/admin/superadmin/instructions/import` | Superadmin | Yes | `super_admin` | Import Instructions |
| `GET` | `/api/v1/admin/superadmin/instructions/{instruction_id}` | Superadmin | Yes | `super_admin` | Get Instruction |
| `PATCH` | `/api/v1/admin/superadmin/instructions/{instruction_id}` | Superadmin | Yes | `super_admin` | Update Instruction |
| `POST` | `/api/v1/admin/superadmin/instructions/{instruction_id}/activate` | Superadmin | Yes | `super_admin` | Activate Instruction |
| `POST` | `/api/v1/admin/superadmin/instructions/{instruction_id}/archive` | Superadmin | Yes | `super_admin` | Archive Instruction |
| `POST` | `/api/v1/admin/superadmin/instructions/{instruction_id}/rollback` | Superadmin | Yes | `super_admin` | Rollback Instruction |
| `GET` | `/api/v1/admin/superadmin/remarks` | Superadmin | Yes | `super_admin` | List Remarks |
| `POST` | `/api/v1/admin/superadmin/remarks` | Superadmin | Yes | `super_admin` | Create Remark |
| `POST` | `/api/v1/admin/superadmin/remarks/{remark_id}/convert` | Superadmin | Yes | `super_admin` | Convert Remark |
| `POST` | `/api/v1/admin/superadmin/remarks/{remark_id}/dismiss` | Superadmin | Yes | `super_admin` | Dismiss Remark |
| `GET` | `/api/v1/admin/data-history` | Property | Yes | `worker, super_admin` | Returns chronological history of who updated which data acro |
| `GET` | `/api/v1/admin/operations/history` | Property | Yes | `worker, super_admin` | Returns full history of all past batch operations, showing a |
| `POST` | `/api/v1/admin/operations/{operation_id}/undo` | Property | Yes | `worker, super_admin` | Instantly rolls back a previous batch operation within its 6 |
| `GET` | `/api/v1/admin/properties` | Property | Yes | `worker, super_admin` | Fetches property listings strictly scoped to the staff membe |
| `POST` | `/api/v1/admin/properties` | Property | Yes | `worker, super_admin` | Create a property in Postgres and Firestore after validation |
| `POST` | `/api/v1/admin/properties/batch-file` | Property | Yes | `worker, super_admin` | Parses the uploaded file and STAGES the bulk change as a pen |
| `DELETE` | `/api/v1/admin/properties/{property_id}` | Property | Yes | `worker, super_admin` | Soft-delete (archive) a property, or permanently delete it w |
| `GET` | `/api/v1/admin/properties/{property_id}` | Property | Yes | `worker, super_admin` | Retrieves a single property listing from Cloud Firestore (pr |
| `PUT` | `/api/v1/admin/properties/{property_id}` | Property | Yes | `worker, super_admin` | Update a property that already exists in Firestore, Postgres |
| `GET` | `/api/v1/admin/properties/{property_id}/consistency` | Property | Yes | `worker, super_admin` | Admin-only comparison of the Firestore document and the Post |
| `GET` | `/api/v1/admin/activity-logs` | System | Yes | `worker, super_admin` | Returns full administrative activity logs. |
| `POST` | `/api/v1/admin/maintenance/purge-expired` | System | Yes | `super_admin` | Admin-only: delete expired undo snapshots from Firestore adm |
| `GET` | `/api/v1/admin/system/config-status` | System | Yes | `worker, super_admin` | Admin-only: feature-flag / wiring status as labels and boole |

---

## 5. PUBLIC CUSTOMER CHATBOT & LEADS

### `GET /api/v1/chat/brochure/file/{filename}`
**Summary:** Get Local Stored Brochure File  
**Authentication:** No | **Authorized Roles:** `public`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `filename` | `path` | `string` | Yes | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/chat/brochure/file/{filename}" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/chat/brochure/shortlist`
**Summary:** Download Property Shortlist PDF  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Generates and downloads a professional PDF comparing and shortlisting selected properties.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_ids` | `query` | `string` | Yes | Query/path parameter |
| `lang` | `query` | `string` | No | Query/path parameter |
| `site_origin` | `query` | `string` | No | Query/path parameter |
| `client_name` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/chat/brochure/shortlist" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/chat/brochure/{property_id}`
**Summary:** Download Property Brochure PDF  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Generates and downloads a professional property brochure PDF with images, specs, highlights, and amenities.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `site_origin` | `query` | `string` | No | Query/path parameter |
| `session_id` | `query` | `string` | No | Query/path parameter |
| `format` | `query` | `string` | No | Query/path parameter |
| `template` | `query` | `string` | No | Query/path parameter |
| `lang` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/chat/brochure/{property_id}" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/chat/contact-info`
**Summary:** Contact numbers and localized copy for the Inquire form  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Returns the public phone / WhatsApp numbers of the site and the localized texts of the Inquire pop-up form.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `site_origin` | `query` | `string` | No | Query/path parameter |
| `language` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/chat/contact-info" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/chat/history/{session_id}`
**Summary:** Get Chat History  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Restores conversation history and saved preferences for a session.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `session_id` | `path` | `string` | Yes | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/chat/history/{session_id}" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/chat/lead`
**Summary:** Submit Direct Lead  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Captures customer contact details directly from modal or callback forms.  

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/chat/lead" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/chat/public`
**Summary:** Public Chat & Lead Capture  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Unified conversational & lead capture endpoint for visitors on tajlandia.com, thai.estate, and varsoviaestate.com.  

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/chat/public" \
  -H "Content-Type: application/json"
```

---

## 6. PROPERTY CATALOG & PUBLIC SEARCH

### `GET /api/v1/branding`
**Summary:** Get Branding  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Returns active tenant branding, bot name, and contact details.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `site_origin` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/branding" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/properties`
**Summary:** List Properties  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Direct property filtering endpoint using safe SQLAlchemy query builder with website scoping.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `location` | `query` | `string` | No | Query/path parameter |
| `property_type` | `query` | `string` | No | Query/path parameter |
| `min_price` | `query` | `string` | No | Query/path parameter |
| `max_price` | `query` | `string` | No | Query/path parameter |
| `bedrooms` | `query` | `string` | No | Query/path parameter |
| `has_pool` | `query` | `string` | No | Query/path parameter |
| `beach_access` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `websites` | `query` | `string` | No | Query/path parameter |
| `site_origin` | `query` | `string` | No | Query/path parameter |
| `limit` | `query` | `string` | No | Query/path parameter |
| `offset` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/properties" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/properties/{property_id}`
**Summary:** Get Property  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Lookup property by exact Property ID with optional website scope check.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `websites` | `query` | `string` | No | Query/path parameter |
| `site_origin` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/properties/{property_id}" \
  -H "Content-Type: application/json"
```

---
### `GET /health`
**Summary:** Health Check  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** System health check, PostgreSQL (admin and user instances), and Firestore connection test.  

**Response Status Codes:** `200`

```bash
curl -X GET "http://127.0.0.1:8000/health" \
  -H "Content-Type: application/json"
```

---

## 7. AUTHENTICATION & IDENTITY MANAGEMENT

### `POST /api/admin/auth/login`
**Summary:** User Login  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Authenticates an administrative / staff user with email and password,  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/admin/auth/login" \
  -H "Content-Type: application/json"
```

---
### `POST /api/admin/auth/register`
**Summary:** Register User  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Multi-user registration endpoint.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/admin/auth/register" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/change-password`
**Summary:** Change Password Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Changes the authenticated administrator's password.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/change-password" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/forgot-password`
**Summary:** Forgot Password Endpoint  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Generates a secure password reset token for an administrator account.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/forgot-password" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/google/callback`
**Summary:** Google Oauth Callback  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Receives authorization code from Google, exchanges it for access & refresh tokens,  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `code` | `query` | `string` | No | Query/path parameter |
| `state` | `query` | `string` | No | Query/path parameter |
| `error` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/google/callback" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/google/disconnect`
**Summary:** Disconnect Google Oauth  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Revokes and unlinks the connected Google account for the authenticated staff member.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/google/disconnect" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/google/login`
**Summary:** Google Oauth Login  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Generates the Google OAuth2 authorization URL for the authenticated staff member.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `redirect_mode` | `query` | `boolean` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/google/login" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/google/status`
**Summary:** Get Google Status  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Returns the Google Workspace connection status for the authenticated user.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/google/status" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/login`
**Summary:** User Login  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Authenticates an administrative / staff user with email and password,  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/login" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/logout`
**Summary:** User Logout  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Terminates the active user session and revokes the token.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/logout" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/me`
**Summary:** Get Current User Profile  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Returns the authenticated user's profile based on the active session token,  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/register`
**Summary:** Register User  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Multi-user registration endpoint.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/register" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/reset-password`
**Summary:** Reset Password Endpoint  
**Authentication:** No | **Authorized Roles:** `public`  
**Description:** Resets the administrator password using a valid cryptographic reset token.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/reset-password" \
  -H "Content-Type: application/json"
```

---

## 8. ADMIN STAFF & USER DIRECTORY

### `GET /api/v1/admin/users`
**Summary:** List Admin Users With Google Status  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Directory endpoint for admins and super admins to inspect staff users.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `role` | `query` | `string` | No | Query/path parameter |
| `search` | `query` | `string` | No | Query/path parameter |
| `is_active` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/users`
**Summary:** Create Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Creates a new administrator or worker user account.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `DELETE /api/v1/admin/users/{user_id}`
**Summary:** Delete Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Deletes a user account and cascades active sessions.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/users/{user_id}`
**Summary:** Get Admin User Details  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Retrieves details and status for a specific user account.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/admin/users/{user_id}`
**Summary:** Update Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Updates user details (name, password reset, role, active status, domain, allowed_websites).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PUT /api/v1/admin/users/{user_id}`
**Summary:** Update Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Updates user details (name, password reset, role, active status, domain, allowed_websites).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PUT "http://127.0.0.1:8000/api/v1/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/admin/users`
**Summary:** List Admin Users With Google Status  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Directory endpoint for admins and super admins to inspect staff users.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `role` | `query` | `string` | No | Query/path parameter |
| `search` | `query` | `string` | No | Query/path parameter |
| `is_active` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/admin/users`
**Summary:** Create Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Creates a new administrator or worker user account.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/admin/users" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `DELETE /api/v1/auth/admin/users/{user_id}`
**Summary:** Delete Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Deletes a user account and cascades active sessions.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/auth/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/auth/admin/users/{user_id}`
**Summary:** Get Admin User Details  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Retrieves details and status for a specific user account.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/auth/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/auth/admin/users/{user_id}`
**Summary:** Update Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Updates user details (name, password reset, role, active status, domain, allowed_websites).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/auth/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PUT /api/v1/auth/admin/users/{user_id}`
**Summary:** Update Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Updates user details (name, password reset, role, active status, domain, allowed_websites).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PUT "http://127.0.0.1:8000/api/v1/auth/admin/users/{user_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/auth/admin/users/{user_id}/block`
**Summary:** Block Admin User  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Super Admin API to block or unblock any administrator or worker user.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `user_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/auth/admin/users/{user_id}/block" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 9. INTERNAL STAFF CHATBOT AGENT

### `POST /api/v1/admin/chat`
**Summary:** Admin Chat Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Authenticated Admin / Internal Team AI Agent unified endpoint.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `stream` | `query` | `boolean` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat/history/{session_id}`
**Summary:** Get Admin History Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Retrieves full admin multi-turn conversation history from Firestore.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `session_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat/history/{session_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 10. TWO-STEP STAGED ACTION GATE (HITL GOVERNANCE)

### `GET /api/v1/admin/actions/{pending_action_id}`
**Summary:** Inspect Pending Action Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Retrieves current status and payload details of a staged pending action.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `pending_action_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/actions/{pending_action_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/actions/{pending_action_id}/confirm`
**Summary:** Confirm Pending Action Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Explicit Two-Step Confirmation endpoint for write actions.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `pending_action_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/actions/{pending_action_id}/confirm" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/actions/{pending_action_id}/reject`
**Summary:** Reject Pending Action Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Rejects a staged pending action and logs the rejection in audit logs.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `pending_action_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/actions/{pending_action_id}/reject" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 11. DOCUMENT & REPORT GENERATION (PDF, XLSX, DOCX)

### `GET /api/v1/admin/brochure/preview`
**Summary:** Admin Brochure Preview  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Admin-only design preview: return a single-property brochure PDF for a chosen theme.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `query` | `string` | Yes | Query/path parameter |
| `template` | `query` | `integer` | No | Query/path parameter |
| `lang` | `query` | `string` | No | Query/path parameter |
| `site_origin` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/brochure/preview" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/reports/download/{token}`
**Summary:** Download Report Signed Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Public (token-gated) download — no Bearer header required.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `token` | `path` | `string` | Yes | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/reports/download/{token}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/reports/{filename}`
**Summary:** Download Report Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Downloads a generated PDF/XLSX/DOCX/CSV property intelligence report (auth required).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `filename` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/reports/{filename}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/reports/{filename}/link`
**Summary:** Create Report Signed Link Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Return a short-lived HMAC signed download URL (valid ~10 minutes).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `filename` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/reports/{filename}/link" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 12. QUERY LOGS, ANALYTICS & ANSWER CORRECTIONS (N3, N4, N5)

### `GET /api/v1/admin/chat-logs/conversations`
**Summary:** List Logged Conversations  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Conversations the caller may see, filtered, searched, sorted and paged on the server.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `agent_type` | `query` | `string` | No | Query/path parameter |
| `role` | `query` | `string` | No | Query/path parameter |
| `user` | `query` | `string` | No | Query/path parameter |
| `website` | `query` | `string` | No | Query/path parameter |
| `conversation_id` | `query` | `string` | No | Query/path parameter |
| `property_id` | `query` | `string` | No | Query/path parameter |
| `remark_status` | `query` | `string` | No | Query/path parameter |
| `q` | `query` | `string` | No | Query/path parameter |
| `date_from` | `query` | `string` | No | Query/path parameter |
| `date_to` | `query` | `string` | No | Query/path parameter |
| `sort` | `query` | `string` | No | Query/path parameter |
| `order` | `query` | `string` | No | Query/path parameter |
| `page` | `query` | `integer` | No | Query/path parameter |
| `page_size` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/conversations" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/conversations/{conversation_id}`
**Summary:** Open Logged Conversation  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** One conversation with its messages in chronological order.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `page` | `query` | `integer` | No | Query/path parameter |
| `page_size` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/conversations/{conversation_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_id}/remarks`
**Summary:** Add Remark  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** An internal remark on one bot response. It never changes what the bot says.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `message_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_id}/remarks" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_seq}/feedback`
**Summary:** Submit User Feedback  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Adds user feedback (thumbs_up/thumbs_down) to a logged assistant message.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `message_seq` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/admin/chat-logs/conversations/{conversation_id}/messages/{message_seq}/feedback" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/corrections`
**Summary:** List Corrections  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `status` | `query` | `string` | No | Query/path parameter |
| `conversation_id` | `query` | `string` | No | Query/path parameter |
| `page` | `query` | `integer` | No | Query/path parameter |
| `page_size` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/corrections/{correction_id}`
**Summary:** Get Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/admin/chat-logs/corrections/{correction_id}`
**Summary:** Edit Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Editing returns the correction to 'proposed': an edited answer has to be approved again.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/chat-logs/corrections/{correction_id}/approve`
**Summary:** Approve Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}/approve" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/chat-logs/corrections/{correction_id}/deactivate`
**Summary:** Deactivate Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}/deactivate" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/chat-logs/corrections/{correction_id}/reject`
**Summary:** Reject Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}/reject" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/corrections/{correction_id}/revisions`
**Summary:** Correction Revisions  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `correction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/corrections/{correction_id}/revisions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/remarks`
**Summary:** List Remarks  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `query` | `string` | No | Query/path parameter |
| `message_id` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `author` | `query` | `string` | No | Query/path parameter |
| `page` | `query` | `integer` | No | Query/path parameter |
| `page_size` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/remarks" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/remarks/{remark_id}`
**Summary:** Get Remark  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/remarks/{remark_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/admin/chat-logs/remarks/{remark_id}`
**Summary:** Edit Remark  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** The author (or a reviewer) edits the text; only a reviewer changes the status. Every change is a revision.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/admin/chat-logs/remarks/{remark_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/chat-logs/remarks/{remark_id}/correction`
**Summary:** Propose Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Propose a corrected answer. It is only a proposal until a super admin approves it.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/chat-logs/remarks/{remark_id}/correction" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/chat-logs/remarks/{remark_id}/revisions`
**Summary:** Remark Revisions  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/chat-logs/remarks/{remark_id}/revisions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/corrections`
**Summary:** Create Admin Correction  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Create a new answer correction directly (without going through a chat log remark).  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/corrections" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 13. SUPERADMIN BUSINESS INSTRUCTIONS & KNOWLEDGE TEACHING (N7)

### `GET /api/v1/admin/superadmin/conversations`
**Summary:** List Conversation Logs  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  
**Description:** The list the Admin Panel's chat-log page loads. It reads the same chat log as /admin/chat-logs/conversations.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `search` | `query` | `string` | No | Query/path parameter |
| `role` | `query` | `string` | No | Query/path parameter |
| `model` | `query` | `string` | No | Query/path parameter |
| `review_status` | `query` | `string` | No | Query/path parameter |
| `website` | `query` | `string` | No | Query/path parameter |
| `start_date` | `query` | `string` | No | Query/path parameter |
| `end_date` | `query` | `string` | No | Query/path parameter |
| `limit` | `query` | `integer` | No | Query/path parameter |
| `offset` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/conversations" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/conversations/{conversation_id}`
**Summary:** Read Conversation Review  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  
**Description:** Read-only chat, pending actions and audit rows. Nothing is written.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/conversations/{conversation_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/conversations/{conversation_id}/review`
**Summary:** Get Conversation Review State  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  
**Description:** What the 'Review AI Response' dialog shows: the full question, the full AI response and whether it is reviewed.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `message_index` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/conversations/{conversation_id}/review" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/conversations/{conversation_id}/review`
**Summary:** Save Conversation Review  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  
**Description:** Save Review. Either tick 'satisfactory', or give instructions, which become an active instruction for that bot and website.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `conversation_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/conversations/{conversation_id}/review" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/instructions`
**Summary:** List Instructions  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `status` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |
| `website` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/instructions`
**Summary:** Create Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/instructions/export`
**Summary:** Export Instructions  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/export" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/instructions/import`
**Summary:** Import Instructions  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/import" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/instructions/{instruction_id}`
**Summary:** Get Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `instruction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/{instruction_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PATCH /api/v1/admin/superadmin/instructions/{instruction_id}`
**Summary:** Update Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `instruction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/{instruction_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/instructions/{instruction_id}/activate`
**Summary:** Activate Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `instruction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/{instruction_id}/activate" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/instructions/{instruction_id}/archive`
**Summary:** Archive Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `instruction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/{instruction_id}/archive" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/instructions/{instruction_id}/rollback`
**Summary:** Rollback Instruction  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `instruction_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/instructions/{instruction_id}/rollback" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/superadmin/remarks`
**Summary:** List Remarks  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `status` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/superadmin/remarks" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/remarks`
**Summary:** Create Remark  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/remarks" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/remarks/{remark_id}/convert`
**Summary:** Convert Remark  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/remarks/{remark_id}/convert" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/superadmin/remarks/{remark_id}/dismiss`
**Summary:** Dismiss Remark  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `remark_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/superadmin/remarks/{remark_id}/dismiss" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 14. PROPERTY INVENTORY MANAGEMENT & BATCH UNDO (C1, C2)

### `GET /api/v1/admin/data-history`
**Summary:** Get Data Change History Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Returns chronological history of who updated which data across the platform.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target_type` | `query` | `string` | No | Query/path parameter |
| `target_id` | `query` | `string` | No | Query/path parameter |
| `updated_by` | `query` | `string` | No | Query/path parameter |
| `limit` | `query` | `integer` | No | Query/path parameter |
| `offset` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/data-history" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/operations/history`
**Summary:** Get Batch Operations History Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Returns full history of all past batch operations, showing affected property counts,  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `limit` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/operations/history" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/operations/{operation_id}/undo`
**Summary:** Undo Batch Operation Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Instantly rolls back a previous batch operation within its 6-day retention window.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `operation_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/operations/{operation_id}/undo" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/properties`
**Summary:** List Admin Properties Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Fetches property listings strictly scoped to the staff member's currently selected websites.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `location` | `query` | `string` | No | Query/path parameter |
| `property_type` | `query` | `string` | No | Query/path parameter |
| `min_price` | `query` | `string` | No | Query/path parameter |
| `max_price` | `query` | `string` | No | Query/path parameter |
| `bedrooms` | `query` | `string` | No | Query/path parameter |
| `has_pool` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `limit` | `query` | `integer` | No | Query/path parameter |
| `offset` | `query` | `integer` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/properties" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/properties`
**Summary:** Create Property Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Create a property in Postgres and Firestore after validation.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `overwrite` | `query` | `string` | No | Query/path parameter |
| `allow_without_undo` | `query` | `boolean` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `201`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/properties" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/properties/batch-file`
**Summary:** Propose Batch File Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Parses the uploaded file and STAGES the bulk change as a pending action with a preview of every row.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `202`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/properties/batch-file" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `DELETE /api/v1/admin/properties/{property_id}`
**Summary:** Delete Property Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Soft-delete (archive) a property, or permanently delete it when hard_delete=true  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `hard_delete` | `query` | `boolean` | No | Query/path parameter |
| `confirm_property_id` | `query` | `string` | No | Query/path parameter |
| `allow_without_undo` | `query` | `boolean` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/admin/properties/{property_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/properties/{property_id}`
**Summary:** Get Admin Property Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Retrieves a single property listing from Cloud Firestore (primary) or local DB.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/properties/{property_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `PUT /api/v1/admin/properties/{property_id}`
**Summary:** Update Property Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Update a property that already exists in Firestore, Postgres, or both.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `allow_without_undo` | `query` | `boolean` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Request Body:** `application/json` (Required)

**Response Status Codes:** `200`, `422`

```bash
curl -X PUT "http://127.0.0.1:8000/api/v1/admin/properties/{property_id}" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/properties/{property_id}/consistency`
**Summary:** Property Consistency Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Admin-only comparison of the Firestore document and the Postgres row.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `property_id` | `path` | `string` | Yes | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/properties/{property_id}/consistency" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 15. SYSTEM DIAGNOSTICS, AUDIT LOGS & MAINTENANCE (C3, C4)

### `GET /api/v1/admin/activity-logs`
**Summary:** Get Admin Activity Logs Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Returns full administrative activity logs.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `admin_email` | `query` | `string` | No | Query/path parameter |
| `action` | `query` | `string` | No | Query/path parameter |
| `status` | `query` | `string` | No | Query/path parameter |
| `start_date` | `query` | `string` | No | Query/path parameter |
| `end_date` | `query` | `string` | No | Query/path parameter |
| `limit` | `query` | `integer` | No | Query/path parameter |
| `offset` | `query` | `integer` | No | Query/path parameter |
| `source` | `query` | `string` | No | Query/path parameter |
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/activity-logs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `POST /api/v1/admin/maintenance/purge-expired`
**Summary:** Purge Expired Snapshots Endpoint  
**Authentication:** Yes | **Authorized Roles:** `super_admin`  
**Description:** Admin-only: delete expired undo snapshots from Firestore admin_batch_operations  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/maintenance/purge-expired" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---
### `GET /api/v1/admin/system/config-status`
**Summary:** Admin Config Status Endpoint  
**Authentication:** Yes | **Authorized Roles:** `worker, super_admin`  
**Description:** Admin-only: feature-flag / wiring status as labels and booleans only.  

**Parameters:**
| Parameter | In | Type | Required | Description |
|:---|:---:|:---:|:---:|:---|
| `target` | `query` | `string` | No | Query/path parameter |

**Response Status Codes:** `200`, `422`

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/admin/system/config-status" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json"
```

---

## 16. STANDARD ERROR RESPONSES & STATUS CODES

The API standardizes error outputs following RFC 7807 problem details:
```json
{
  "detail": "Descriptive message identifying the cause of validation or permission error",
  "error_code": "RESOURCE_NOT_FOUND",
  "timestamp": "2026-10-30T16:20:00Z"
}
```

| Status Code | HTTP Reason | Meaning in Platform |
|:---|:---|:---|
| **`200 OK`** | Success | Request succeeded and returned requested payload. |
| **`201 Created`** | Created | Resource successfully created (e.g. staff user registration, lead saved). |
| **`400 Bad Request`** | Client Error | Malformed JSON, failed Pydantic schema validation, or invalid confirmation token. |
| **`401 Unauthorized`** | Auth Required | Missing, expired, or invalid JWT bearer token. |
| **`403 Forbidden`** | Permission Denied | Authenticated role (`worker`) lacks privilege for target action (e.g., purge maintenance). |
| **`404 Not Found`** | Not Found | Property ID, conversation session, or staged pending action does not exist. |
| **`422 Unprocessable`** | Schema Violation | Request parameters failed type or constraint checks. |
| **`500 Internal Error`** | Server Error | Unhandled backend exception (automatically audited to Firestore). |
| **`503 Unavailable`** | Service Down | Staging PostgreSQL or Firestore unreachable. |