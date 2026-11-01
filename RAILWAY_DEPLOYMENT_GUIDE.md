# 🚀 Railway Deployment Guide for Tajlandia AI Platform

This guide walks you through deploying the complete **Tajlandia.com AI Platform** (FastAPI backend, PostgreSQL database, OpenAI Public Chatbot, Claude Admin Advisor, Google Maps, Calendar, and Gmail integrations) to **Railway**.

---

## Prerequisites
* A [Railway account](https://railway.app/) (Free or Hobby plan).
* Your API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and optional Google keys).

---

## Step 1: Deploy with GitHub (Recommended)

### 1. Push your project to GitHub
Commit and push your current codebase to your GitHub repository:
```bash
git add .
git commit -m "feat: complete Tajlandia AI platform with Maps, Calendar, and Gmail"
git push origin main
```

### 2. Create a new project in Railway
1. Go to [railway.app/dashboard](https://railway.app/dashboard).
2. Click **"+ New Project"**.
3. Select **"Deploy from GitHub repo"**.
4. Choose your `tajlandia` repository.

---

## Step 2: Firestore

The app uses Cloud Firestore only. No SQL database is needed. Set the `ADMIN_FIREBASE_*` and `USER_FIREBASE_*` variables from your Firebase service account.

---

## Step 3: Configure Environment Variables in Railway

In your Railway web service $\rightarrow$ go to **Variables** $\rightarrow$ click **"RAW Editor"** (or add one by one):

```env
ENVIRONMENT=production
OPENAI_API_KEY=sk-proj-your-openai-api-key
OPENAI_MODEL=gpt-4o-mini
OPENAI_TEMPERATURE=0.2

ANTHROPIC_API_KEY=sk-ant-api03-your-anthropic-api-key
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
ANTHROPIC_TEMPERATURE=0.2

INTERNAL_API_SECRET=tajlandia-prod-secret-token

# Google Maps Platform (Optional: displays custom map markers)
GOOGLE_MAPS_API_KEY=AIzaSy_your_google_maps_key

# Gmail Dispatch (Recommended: 2-minute setup via Google App Password)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your_16_character_app_password
TAJLANDIA_SALES_EMAIL=sales@tajlandia.com

# Google OAuth2 (For Google Calendar & Gmail API)
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-client-secret
GOOGLE_REDIRECT_URI=https://${{RAILWAY_PUBLIC_DOMAIN}}/api/v1/auth/google/callback
```

---

## Step 4: Generate Public Domain & Launch

1. Go to your Web Service card $\rightarrow$ click the **"Settings"** tab.
2. Scroll to the **"Networking"** section.
3. Click **"Generate Domain"** (e.g. `tajlandia-ai-production.up.railway.app`).
4. Railway will automatically trigger a deployment.

### What Railway Does Automatically on Startup:
1. Builds the application using `railway.json` and `Dockerfile`.
2. Starts Uvicorn listening on Railway's dynamic `${PORT}`.
3. Verifies health status via `GET /health`.

---

## Step 5: Test Your Live App

1. Open your Railway public domain:
   ```text
   https://your-domain.up.railway.app/
   ```
2. **User Discovery Chatbot:**
   * Test: *"Show me 3-bedroom pool villas in Phuket under 30 million THB"*
   * Inspect the live **Focus Bar**, **What We Fetched** drawer, and **Property Cards**.
   * Click **`📍 Map`** to verify Google Maps coordinates and landmark proximity.
   * Click **`📅 Tour`** to verify Google Calendar viewing booking.
   * Click **`✉️ Email`** to test emailing specs via Gmail.
3. **Admin Advisor Chatbot:**
   * Toggle the top switcher to **Admin Advisor**.
   * Test: *"What is the roof material of PROP-PHK-001?"*
   * Test: *"Generate a PDF report of our top villas"*
