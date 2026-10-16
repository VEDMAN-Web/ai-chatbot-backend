# Frontend Developer API Guide (Client-Facing Only)

This guide contains **only the 7 client-facing APIs** needed to build the website, property catalog, and chatbot UI. No backend or admin endpoints.

---

## Quick Reference Table

| # | Feature | Route | Method | What It Does |
|---|---|---|---|---|
| 1 | **Website Branding** | `/api/v1/branding` | `GET` | Get bot name, brand title, and advisor email for active site |
| 2 | **Unified Chat & Contact API** | `/api/v1/chat/public` | `POST` | **Single endpoint** for both user chat and contact/lead form submission |
| 3 | **Chat History** | `/api/v1/chat/history/{session_id}` | `GET` | Load previous chat messages if user refreshes page |
| 4 | **Brochure Download** | `/api/v1/chat/brochure/{property_id}` | `GET` | Download A4 luxury property PDF brochure |
| 5 | **Property Catalog** | `/api/v1/properties` | `GET` | Filter listings by location, price, bedrooms, pool, etc. |
| 6 | **Single Property** | `/api/v1/properties/{property_id}` | `GET` | Get full specifications, photos, and GPS coordinates |
| 7 | *(Optional)* Dedicated Lead API | `/api/v1/chat/lead` | `POST` | Standalone contact submission endpoint (or use `/chat/public`) |

---

## 1. Website Branding & Bot Identity

### `GET /api/v1/branding`
Call this when the page loads to show the correct Bot Name, Website Title, and Contact Email (e.g. for `tajlandia.com`, `thai.estate`, or `varsoviaestate.com`).

#### What to Send:
- **Query Parameter (optional):**
  - `site_origin`: e.g. `thai.estate` or `varsoviaestate.com` (if omitted, server defaults to `tajlandia.com`)

#### Success Response (`200 OK`):
```json
{
  "site_origin": "thai.estate",
  "brand_name": "Thai Estate",
  "bot_name": "Thai Estate Advisor",
  "brand_title": "Thai Estate — Luxury Thailand Real Estate",
  "brand_subtitle": "Luxury Real Estate",
  "domain": "thai.estate",
  "advisor_email": "advisor@thai.estate",
  "website_url": "https://thai.estate"
}
```

#### Where It Fails:
* **Never fails with 400/404**: If an unknown domain is passed, it automatically falls back to `tajlandia.com` default.
* If the server is offline: Network Error.

---

## 2. Unified Customer Chatbot & Contact Submission

### `POST /api/v1/chat/public`
**Use this ONE API for everything:**
1. Chatting with the AI advisor (asking for villas, prices, locations).
2. Submitting contact forms (callback request, private tour booking, inquiry).
3. Both at the same time (user asking a question while providing their phone/email).

---

### Scenario A: Normal Chat Question

#### What to Send (`POST JSON`):
```json
{
  "session_id": "client_sess_101",
  "message": "Show me 3-bedroom pool villas in Bang Tao under 25M THB",
  "site_origin": "tajlandia.com",
  "language": "en"
}
```

---

### Scenario B: Contact Form Submission (Lead Capture)
When the user submits a modal form, viewing request, or callback request, send to the **same** endpoint:

#### What to Send (`POST JSON`):
```json
{
  "session_id": "client_sess_101",
  "contact_name": "Alexander Smith",
  "contact_email": "alexander.smith@example.com",
  "contact_phone": "+48 501 234 567",
  "preferred_location": "Bang Tao, Phuket",
  "budget": 25000000,
  "notes": "Interested in viewing next week",
  "site_origin": "tajlandia.com",
  "language": "en"
}
```
*(Notice: `message` is optional when contact fields are present!)*

#### Field Details:
* `session_id` (string, **required**): Unique ID for the visitor session.
* `message` (string, optional*): Visitor query text.
* `contact_name` (string, optional*): Visitor's full name.
* `contact_email` (string, optional*): Visitor's email address.
* `contact_phone` (string, optional*): Visitor's phone or WhatsApp number.
  *(**Rule:** Either `message` OR at least one contact field — name, email, or phone — must be provided).*
* `preferred_location` (string, optional): Target area (e.g. `"Bang Tao, Phuket"`).
* `budget` (number, optional): Numeric budget in THB (e.g. `25000000`).
* `notes` (string, optional): Freeform inquiry notes.
* `site_origin` (string, optional): Origin domain (`tajlandia.com`, `thai.estate`, or `varsoviaestate.com`).
* `language` (string, optional): `"en"`, `"pl"`, or `"th"`. Default is `"en"`.

---

#### Success Response 1 — Contact Form Submission (`200 OK`):
```json
{
  "type": "text",
  "text": "Thank you, Alexander Smith! Your inquiry has been registered. An advisor from Tajlandia AI (advisor@tajlandia.com) will contact you shortly.",
  "properties": [],
  "quick_replies": [
    "Show me properties in Phuket",
    "Search villas under 15M THB"
  ],
  "lead_captured": true,
  "lead_id": "b182cb82-5813-4dc9-8fa9-93e18a99478e",
  "preferences": {},
  "session_id": "client_sess_101",
  "conversation_id": "940adccc-4baf-42e7-adcf-8ccb58af809c",
  "language": "en",
  "brochure_property_id": null
}
```
* `lead_captured` is `true`.
* `lead_id` contains the unique database ID of the registered lead.
* `text` contains the branded confirmation in the requested language (EN, PL, or TH).

---

#### Success Response 2 — Property Search Cards (`200 OK`):
```json
{
  "type": "property_cards",
  "text": "Here are verified luxury pool villas in Bang Tao matching your criteria:",
  "properties": [
    {
      "property_id": "PROP-PHK-001",
      "name": "Aileen Residence Bangtao",
      "property_type": "Villa",
      "price": "from 8.xx million THB",
      "bedrooms": "2–3",
      "area": "167–242 sqm",
      "location": "Bang Tao, Phuket",
      "status": "Under construction",
      "has_pool": true,
      "beach_access": false,
      "images": [
        "https://images.unsplash.com/photo-1613977257363-707ba9348227?auto=format&fit=crop&w=1200&q=80"
      ],
      "detail_page_url": "https://tajlandia.com/oferty/phuket/aileen-residence-villas",
      "highlights": [
        "Private swimming pool with sun terrace",
        "5 minutes drive to Bang Tao Beach"
      ]
    }
  ],
  "quick_replies": ["Villas under 15M THB", "With Private Pool", "Show more in Phuket"],
  "lead_captured": false,
  "lead_id": null,
  "preferences": {
    "location": "Bang Tao",
    "property_type": "Villa",
    "bedrooms": 3,
    "max_price": 25000000
  },
  "session_id": "client_sess_101",
  "conversation_id": "940adccc-4baf-42e7-adcf-8ccb58af809c",
  "language": "en",
  "brochure_property_id": null
}
```

---

#### Success Response 3 — When User Asks for Brochure (`200 OK`):
```json
{
  "type": "brochure",
  "text": "📄 Generating official brochure for Aileen Residence Bangtao — your download will start automatically.",
  "properties": [],
  "quick_replies": [],
  "lead_captured": true,
  "lead_id": null,
  "preferences": {},
  "session_id": "client_sess_101",
  "conversation_id": "940adccc-4baf-42e7-adcf-8ccb58af809c",
  "language": "en",
  "brochure_property_id": "PROP-PHK-001"
}
```
> **Frontend Action:** If `brochure_property_id` is present, automatically trigger:  
> `GET /api/v1/chat/brochure/PROP-PHK-001` to start the download.

---

#### Success Response 4 — Session Message Limit Reached (`200 OK`):
When the visitor has already sent `MAX_USER_MESSAGES_PER_SESSION` chat messages in this `session_id` (default **50**; set to `0` to disable), further chat messages are blocked. The LLM is not called. Contact-form-only submissions (no `message` text) are not counted and are not blocked.

```json
{
  "type": "session_limit",
  "text": "This chat has reached its message limit. Please start a new chat to continue.\n\nOur advisory team (advisor@tajlandia.com) is happy to help if you need further assistance.",
  "properties": [],
  "quick_replies": [],
  "lead_captured": false,
  "lead_id": null,
  "preferences": {
    "session_message_count": 50
  },
  "session_id": "client_sess_101",
  "conversation_id": "940adccc-4baf-42e7-adcf-8ccb58af809c",
  "language": "en",
  "brochure_property_id": null,
  "session_limit_reached": true,
  "message_limit": 50,
  "session_limit_warning": false
}
```

* `type` is `"session_limit"`.
* `session_limit_reached` is `true`.
* `message_limit` is the configured cap.
* `quick_replies` is empty (frontend should show its own "Start new chat" control).
* `text` is localized (`en` / `pl` / `th`) and includes the tenant advisor email for `site_origin`.
* Normal chat replies may also include `session_limit_warning: true` when 5 or fewer messages remain before the limit.

---

#### Where It Fails:
1. **Empty Message & No Contact Information (`400 Bad Request`):**
   * **Why:** Both `message` and all contact fields (`contact_name`, `contact_email`, `contact_phone`) were empty or missing.
   * **Response:**
     ```json
     { "detail": "Please provide a message or contact details." }
     ```
   * **Frontend Handling:** Validate that either the chat text box is not empty or at least one contact input has a value.

2. **Missing `session_id` (`422 Unprocessable Entity`):**
   * **Why:** The JSON payload omitted `session_id`.
   * **Response:**
     ```json
     {
       "detail": [
         { "loc": ["body", "session_id"], "msg": "Field required", "type": "missing" }
       ]
     }
     ```

3. **Invalid Budget Format (`422 Unprocessable Entity`):**
   * **Why:** Sent string instead of a valid number in `budget` (e.g. `"twenty million"`).
   * **Response:**
     ```json
     {
       "detail": [
         { "loc": ["body", "budget"], "msg": "Input should be a valid number", "type": "float_type" }
       ]
     }
     ```

4. **Server Error (`500 Internal Server Error`):**
   * **Why:** Temporary database or service issue.
   * **Response:**
     ```json
     { "detail": "An error occurred while processing your property search request: ..." }
     ```
   * **Frontend Handling:** Display a toast message: *"Service temporarily unavailable. Please try again in a moment."*

---

## 3. Restore Chat History

### `GET /api/v1/chat/history/{session_id}`
Call this when the page loads to restore previous chat messages if the visitor refreshed their browser.

#### What to Send:
- **Path Parameter:** `session_id` (e.g. `/api/v1/chat/history/client_sess_101`)

#### Success Response (`200 OK`):
```json
{
  "session_id": "client_sess_101",
  "conversation_id": "940adccc-4baf-42e7-adcf-8ccb58af809c",
  "site_origin": "tajlandia.com",
  "lead_score": 45.0,
  "preferences": {
    "location": "Phuket",
    "property_type": "Villa"
  },
  "messages": [
    { "role": "user", "content": "Show me villas in Phuket" },
    { "role": "assistant", "content": "Here are available villas...", "properties": [...] }
  ]
}
```

#### Where It Fails:
* **Session has no history (`200 OK`):** Does NOT error. Returns `{ "session_id": "...", "turns_count": 0, "messages": [] }` so the frontend can safely show a clean welcome screen.

---

## 4. (Optional) Dedicated Contact / Lead Submission Form

### `POST /api/v1/chat/lead`
> 💡 **Tip:** You do **not** need this if you use `POST /api/v1/chat/public` (Section 2 Scenario B)! `POST /api/v1/chat/public` now handles both chat messages and contact submissions through one single API.  
> Keep this endpoint only if you prefer a separate dedicated URL for modal contact forms.

Call this when a user submits their contact information via a modal, callback button, or viewing request form.

#### What to Send (`POST JSON`):
```json
{
  "session_id": "client_sess_101",
  "contact_name": "Alexander Smith",
  "contact_email": "alexander.smith@example.com",
  "contact_phone": "+48 501 234 567",
  "preferred_location": "Bang Tao, Phuket",
  "budget": 25000000,
  "notes": "Interested in viewing next week"
}
```
* `session_id` (string, **required**): Connects this lead to their active chat session.
* `contact_name` (string, optional*): Name.
* `contact_email` (string, optional*): Email.
* `contact_phone` (string, optional*): Phone / WhatsApp.
  *(**Must provide at least one**: name, email, or phone).*
* `preferred_location` (string, optional): Target area.
* `budget` (number, optional): Numeric budget in THB.
* `notes` (string, optional): Message or inquiry.

#### Success Response (`200 OK`):
```json
{
  "status": "success",
  "lead_id": "b182cb82-...",
  "lead_score": 75.0,
  "message": "Thank you! Your inquiry has been registered. An advisor will contact you shortly."
}
```

#### Where It Fails:
1. **No Contact Information Provided (`400 Bad Request`):**
   * **Why:** Name, email, and phone were all blank.
   * **Response:**
     ```json
     { "detail": "Please provide at least a name, email, or phone number." }
     ```
   * **Frontend Handling:** Highlight contact fields in red.

2. **Budget is not a number (`422 Unprocessable Entity`):**
   * **Why:** Sent text instead of a number in `budget` (e.g. `"20 million"`).
   * **Response:**
     ```json
     {
       "detail": [
         { "loc": ["body", "budget"], "msg": "value is not a valid float", "type": "type_error.float" }
       ]
     }
     ```

---

## 5. Download Property PDF Brochure

### `GET /api/v1/chat/brochure/{property_id}`
Generates and downloads a branded luxury A4 PDF brochure. Automatically records a high-intent buying signal for agency staff.

#### What to Send:
- **Path Parameter:** `property_id` (required, e.g. `PROP-PHK-001`)
- **Query Parameters (optional):**
  - `site_origin`: `thai.estate`, `varsoviaestate.com`, or `tajlandia.com` (brands the PDF)
  - `session_id`: Active session ID (links download to user's chat)

#### Success Response (`200 OK`):
- **Headers:**
  - `Content-Type: application/pdf`
  - `Content-Disposition: attachment; filename="Tajlandia_AI_Property_PROP-PHK-001.pdf"`
- **Body:** Binary PDF byte stream.

#### Where It Fails:
1. **Property Not Found (`404 Not Found`):**
   * **Why:** The requested property ID does not exist in the database.
   * **Response:**
     ```json
     { "detail": "Property 'PROP-INVALID' not found." }
     ```
   * **Frontend Handling:** Show toast: *"Brochure not found for this property."*

2. **PDF Generation Error (`500 Internal Server Error`):**
   * **Response:**
     ```json
     { "detail": "Failed to generate property brochure. Please try again." }
     ```
   * **Frontend Handling:** Reset button from "⏳ Generating..." to "❌ Try again".

---

## 6. Property Catalog Search & Filter

### `GET /api/v1/properties`
Filters properties from the database without AI. Use this for property grid pages, search filters, and catalog lists.

#### What to Send (All Query Parameters Optional):
| Parameter | Type | Example | Description |
|---|---|---|---|
| `location` | string | `Phuket` or `Bang Tao` | Island or district |
| `property_type` | string | `Villa` or `Condominium` | Category |
| `min_price` | number | `5000000` | Minimum price (THB) |
| `max_price` | number | `20000000` | Maximum price (THB) |
| `bedrooms` | integer | `3` | Exact bedroom count |
| `has_pool` | boolean | `true` | Must have swimming pool |
| `beach_access` | boolean | `true` | Walk to beach |

#### Success Response (`200 OK`):
```json
{
  "count": 1,
  "properties": [
    {
      "property_id": "PROP-PHK-001",
      "property_name": "Aileen Residence Bangtao",
      "property_type": "Villa",
      "status": "Under construction",
      "area": "Bang Tao",
      "district": "Choeng Thale",
      "price_display": "from 8.xx million THB",
      "bedrooms": "2–3",
      "area_sqm": "167–242 sqm",
      "has_pool": true,
      "beach_access": false,
      "images": ["https://images.unsplash.com/..."],
      "detail_page_url": "https://tajlandia.com/oferty/phuket/aileen-residence-villas"
    }
  ]
}
```

#### Where It Fails:
* **Invalid Filter Format (`422 Unprocessable Entity`):**
  * **Why:** Sending letters in numeric filters (e.g. `min_price=cheap`).
  * **Response:**
    ```json
    {
      "detail": [
        { "loc": ["query", "min_price"], "msg": "value is not a valid float", "type": "type_error.float" }
      ]
    }
    ```

---

## 7. Single Property Full Details

### `GET /api/v1/properties/{property_id}`
Returns full record, developer info, GPS coordinates, specs, and gallery for a single property detail page or modal.

#### What to Send:
- **Path Parameter:** `property_id` (required, e.g. `PROP-PHK-001`)

#### Success Response (`200 OK`):
```json
{
  "found": true,
  "property_id": "PROP-PHK-001",
  "property_name": "Aileen Residence Bangtao",
  "project_name": "Aileen Residence",
  "property_type": "Villa",
  "status": "Under construction",
  "location": {
    "country": "Thailand",
    "province": "Phuket",
    "district": "Bang Tao",
    "area": "Bang Tao",
    "address": "Choeng Thale, Thalang District, Phuket 83110",
    "latitude": 7.9942,
    "longitude": 98.3051
  },
  "developer": {
    "name": "Aileen Development Group",
    "description": "Leading luxury residential developer in Phuket."
  },
  "pricing": {
    "display": "from 8.xx million THB",
    "numeric_min": 8500000.0,
    "currency": "THB",
    "note": "Excludes sinking fund"
  },
  "details": {
    "bedrooms": "2–3",
    "bathrooms": "2–3",
    "area": "167–242 sqm",
    "units": 54
  },
  "description": "Exclusive contemporary tropical pool villas located in Bang Tao...",
  "amenities": ["Private Pool", "24/7 Security", "Clubhouse", "Fitness Center"],
  "highlights": ["54 Exclusive villas", "Foreign ownership available"],
  "images": [
    "https://images.unsplash.com/photo-1613977257363-707ba9348227?auto=format&fit=crop&w=1200&q=80"
  ],
  "detail_page_url": "https://tajlandia.com/oferty/phuket/aileen-residence-villas"
}
```

#### Where It Fails:
* **Property Does Not Exist (`404 Not Found`):**
  * **Why:** The requested ID does not exist.
  * **Response:**
    ```json
    { "detail": "Property with ID 'PROP-NONEXISTENT' not found." }
    ```
  * **Frontend Handling:** Redirect to 404 screen or show "Listing not found".
