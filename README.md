# Tajlandia AI Property Platform

A full-stack AI-powered property intelligence system for [Tajlandia.com](https://tajlandia.com) - featuring a **Public Customer Chatbot** and an **Admin / Internal AI Advisor**, built on FastAPI, PostgreSQL, and modern LLM integrations.

---

## Features

### Public Customer Chatbot
- Conversational property search (by location, bedrooms, price, amenities)
- Multilingual support: **English**, **Polish**, **Thai**
- Smart user preference tracking and progressive refinement
- Property cards with images, pricing, and highlights
- Google Maps integration - distances to landmarks and beaches
- Google Calendar- schedule property viewings
- Gmail - email property details directly to the buyer
- Lead capture with intent scoring

### Admin / Internal AI Advisor
- Full access to all properties + private construction specifications
- Analytics and portfolio insights
- Multi-format report generation (PDF, Excel, Word, CSV)
- Google Drive document search and summarization
- Gmail drafting and Calendar scheduling - all with mandatory two-step confirmation
- Immutable audit logging for all admin actions

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.12) |
| Database | PostgreSQL 16+ with pgvector |
| Migrations | Alembic |
| Public LLM | OpenAI (`gpt-4o-mini`) |
| Admin LLM | Anthropic Claude (`claude-sonnet`) |
| Frontend | Vanilla JS + CSS (served via FastAPI static files) |
| Deployment | Docker / Railway |

---

## Project Structure

```
tajlandia/
├── app/
│   ├── api/v1/          # REST API endpoints (chat, admin)
│   ├── core/            # Auth, config, and middleware
│   ├── services/        # AI agents, search, integrations, i18n
│   └── static/          # Frontend UI (HTML, CSS, JS)
├── data/                # Property catalog and knowledge seed data
├── scripts/             # One-off tools (e.g. the SQL-to-Firestore user migration)
├── Dockerfile
├── requirements.txt
├── run.py               # Application entrypoint
└── .env.example         # Environment variable template
```

---

## Getting Started

### 1. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `ADMIN_FIREBASE_*` / `USER_FIREBASE_*` | Firestore service account settings |
| `OPENAI_API_KEY` | OpenAI API key for the public chatbot |
| `ANTHROPIC_API_KEY` | Anthropic API key for the admin advisor |
| `INTERNAL_API_SECRET` | A secure random string for internal API auth |

Optional (for full Google integrations):

| Variable | Description |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Google Maps Platform key |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Gmail SMTP for email dispatch |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth2 for Drive and Calendar |

---

### 2. Run with Docker (Recommended)

```bash
docker build -t tajlandia-app .
docker run -p 8080:8080 --env-file .env tajlandia-app
```

The app will automatically:
1. Run database migrations
2. Seed the property catalog
3. Start the server at `http://localhost:8080`

---

### 3. Run Locally with Python

```bash
pip install -r requirements.txt
python run.py
```

---

## Deployment (Railway)

See [RAILWAY_DEPLOYMENT_GUIDE.md](./RAILWAY_DEPLOYMENT_GUIDE.md) for a step-by-step guide to deploying on Railway with a one-click PostgreSQL database.

---

## Admin replay (staging only)

Vedant can replay the admin conversations against staging with a real model key. Do not point this at production.

```bash
set REPLAY_BASE_URL=https://<staging-host>
set REPLAY_TOKEN=<super-admin bearer token>
python scripts/admin_replay.py
```

The script reads `tests/replay/admin_conversations.yaml`, creates only a property whose name starts with `ZZ_TEST_`, prints a pass/fail scorecard with latency, writes `replay_report.json`, and deletes the test property through the API. It exits immediately if the name does not start with `ZZ_TEST_` or the URL looks like production. Turns written as `Probe Villa` are rewritten to that test name before they are sent, so a real listing is never the target. After a confirmed write it reads the property and the store-consistency endpoint.

## API Reference

Once running, interactive API documentation is available at:
- **Swagger UI:** `http://localhost:8080/docs`
- **ReDoc:** `http://localhost:8080/redoc`
- **Health Check:** `http://localhost:8080/health`
