"""
Google Workspace OAuth2 Authentication Setup Helper
Run this script to authenticate your Gmail and Google Calendar integration.

Prerequisites:
1. Go to Google Cloud Console (https://console.cloud.google.com).
2. Create or select a project (e.g. 'Tajlandia Property Intelligence').
3. Enable 'Gmail API', 'Google Calendar API', and 'Google Drive API'.
4. Configure OAuth Consent Screen:
   - User Type: External or Internal (if using Google Workspace).
   - Scopes:
       - https://www.googleapis.com/auth/gmail.send
       - https://www.googleapis.com/auth/calendar.events
       - https://www.googleapis.com/auth/drive.readonly
5. Go to Credentials -> Create Credentials -> OAuth Client ID:
   - Application Type: Desktop app (or Web application)
   - Download the JSON file and save it as:
     credentials/credentials.json
6. Run:
     python scripts/setup_google_auth.py
"""

import os
import sys

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from google_auth_oauthlib.flow import InstalledAppFlow
from app.services.google_services import SCOPES, CREDENTIALS_DIR

CREDENTIALS_JSON = os.path.join(CREDENTIALS_DIR, "credentials.json")
TOKEN_JSON = os.path.join(CREDENTIALS_DIR, "google_token.json")


def main():
    print("=" * 70)
    print("TAJLANDIA PLATFORM - GOOGLE WORKSPACE OAUTH SETUP")
    print("=" * 70)

    if not os.path.exists(CREDENTIALS_JSON):
        print(f"\n[!] Missing credentials file at:\n    {CREDENTIALS_JSON}\n")
        print("To setup Gmail and Calendar authentication:")
        print("1. Go to https://console.cloud.google.com")
        print("2. Enable Gmail API and Google Calendar API")
        print("3. Create an OAuth 2.0 Client ID (Application type: Desktop App)")
        print(f"4. Download the JSON file and place it at: credentials/credentials.json")
        print("5. Re-run: python scripts/setup_google_auth.py\n")
        print("Alternative (Google Workspace with custom domain @tajlandia.pl):")
        print("Drop your service account JSON file at: credentials/service_account.json")
        return

    print(f"\n[+] Found {CREDENTIALS_JSON}. Starting interactive browser login...")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_JSON, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_JSON, "w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())

    print(f"\n[SUCCESS] Authentication token saved to:\n    {TOKEN_JSON}")
    print("The Tajlandia AI Admin Agent can now send real emails and create calendar viewings!")


if __name__ == "__main__":
    main()
