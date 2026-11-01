import os
import base64
import secrets
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from email.message import EmailMessage
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

CREDENTIALS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "credentials")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly"
]


class GoogleWorkspaceManager:
    """
    Production Google Workspace integration manager for Gmail, Calendar, and Drive.
    Automatically connects via OAuth2 token or Service Account if credentials exist,
    otherwise provides safe simulated mock drivers for offline testing and development.
    """
    def __init__(self):
        self.client_id = settings.GOOGLE_CLIENT_ID
        self.client_secret = settings.GOOGLE_CLIENT_SECRET
        self.credentials_dir = CREDENTIALS_DIR
        os.makedirs(self.credentials_dir, exist_ok=True)

    def get_credentials(self) -> Optional[Any]:
        """
        Attempts to load real Google credentials in priority order:
        1. credentials/google_token.json (User OAuth2 Authorized Token)
        2. credentials/service_account.json (Google Workspace Service Account)
        """
        token_path = os.path.join(self.credentials_dir, "google_token.json")
        if os.path.exists(token_path):
            try:
                from google.oauth2.credentials import Credentials
                creds = Credentials.from_authorized_user_file(token_path, SCOPES)
                if creds and creds.valid:
                    return creds
            except Exception as e:
                logger.warning(f"Error loading google_token.json: {e}")

        sa_path = os.path.join(self.credentials_dir, "service_account.json")
        if os.path.exists(sa_path):
            try:
                from google.oauth2 import service_account
                return service_account.Credentials.from_service_account_file(
                    sa_path,
                    scopes=SCOPES
                )
            except Exception as e:
                logger.warning(f"Error loading service_account.json: {e}")

        return None

    def get_credentials_for_user(
        self,
        db: Optional[Any] = None,
        staff_user_id_or_email: Optional[str] = None
    ) -> Optional[Any]:
        """
        Multi-tenant credential resolver.
        Retrieves user-specific OAuth2 credentials from the user store (preferences["google_workspace"]).
        If the access token is expired, refreshes it automatically and persists the new token in the database.
        Falls back to global credentials if no user credentials are present.
        """
        if staff_user_id_or_email:
            try:
                from app.repositories import users_repo
                clean_target = staff_user_id_or_email.strip()
                user = users_repo.get_by_email(db, clean_target)

                if user and user.preferences:
                    gw = user.preferences.get("google_workspace")
                    if gw and gw.get("connected") and gw.get("refresh_token"):
                        from google.oauth2.credentials import Credentials
                        from google.auth.transport.requests import Request

                        creds = Credentials(
                            token=gw.get("access_token"),
                            refresh_token=gw.get("refresh_token"),
                            token_uri="https://oauth2.googleapis.com/token",
                            client_id=self.client_id,
                            client_secret=self.client_secret,
                            scopes=gw.get("scopes") or SCOPES
                        )

                        # Refresh token if expired or invalid
                        if not creds.valid:
                            try:
                                creds.refresh(Request())
                                prefs = dict(user.preferences)
                                prefs["google_workspace"]["access_token"] = creds.token
                                if creds.expiry:
                                    prefs["google_workspace"]["expires_at"] = int(creds.expiry.timestamp())
                                user.preferences = prefs
                                users_repo.save_user(db, user)
                                logger.info(f"Refreshed Google OAuth token for user '{user.email}'")
                            except Exception as refr_err:
                                logger.warning(f"Could not refresh token for user '{user.email}': {refr_err}")

                        if creds.valid:
                            return creds
            except Exception as e:
                logger.warning(f"Error resolving user Google credentials for '{staff_user_id_or_email}': {e}")

        # A named staff member never silently borrows someone else's Google account (Drive, Gmail, Calendar).
        if staff_user_id_or_email and not getattr(settings, "GOOGLE_SHARED_CREDENTIAL_FALLBACK", False):
            return None
        # System-level credentials: no staff member named (background work), or the shared fallback is switched on
        return self.get_credentials()

    # 1. Gmail Dispatcher
    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        staff_user_email: Optional[str] = None,
        db: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Dispatches email via:
        1. Google Workspace Gmail API (per-user OAuth2 token or service account)
        2. SMTP / Gmail App Password (if SMTP_HOST, SMTP_USER, SMTP_PASSWORD configured)
        3. Simulated Mock Dispatcher (for offline testing & local development)
        """
        creds = self.get_credentials_for_user(db, staff_user_email)
        # Option 1: Gmail API
        if creds:
            try:
                from googleapiclient.discovery import build
                service = build('gmail', 'v1', credentials=creds)

                msg = EmailMessage()
                msg["To"] = to
                msg["From"] = "me"
                msg["Subject"] = subject
                msg.set_content(body)
                if html_body:
                    msg.add_alternative(html_body, subtype='html')

                raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                sent = service.users().messages().send(userId='me', body={'raw': raw_bytes}).execute()
                logger.info(f"[PROD GMAIL API] Sent email to {to} (Message ID: {sent.get('id')})")
                return {
                    "success": True,
                    "message_id": sent.get("id"),
                    "to": to,
                    "subject": subject,
                    "body_preview": body[:120] + "..." if len(body) > 120 else body,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": "gmail_api",
                    "is_mock": False
                }
            except Exception as exc:
                logger.warning(f"[GMAIL API NOTICE] Failed to send email via Gmail API ({exc}). Falling back to SMTP/simulation.")

        # Option 2: SMTP Dispatch (e.g. Gmail App Password)
        if settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD:
            try:
                import smtplib
                from email.mime.multipart import MIMEMultipart
                from email.mime.text import MIMEText

                smtp_msg = MIMEMultipart('alternative')
                smtp_msg['Subject'] = subject
                smtp_msg['From'] = settings.SMTP_USER
                smtp_msg['To'] = to
                smtp_msg.attach(MIMEText(body, 'plain'))
                if html_body:
                    smtp_msg.attach(MIMEText(html_body, 'html'))

                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                    server.starttls()
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    server.send_message(smtp_msg)

                msg_id = f"smtp_{secrets.token_hex(8)}"
                logger.info(f"[SMTP GMAIL] Sent email to {to} via SMTP {settings.SMTP_HOST}")
                return {
                    "success": True,
                    "message_id": msg_id,
                    "to": to,
                    "subject": subject,
                    "body_preview": body[:120] + "..." if len(body) > 120 else body,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": "smtp",
                    "is_mock": False
                }
            except Exception as exc:
                logger.warning(f"[SMTP ERROR] Failed to send email via SMTP: {exc}. Falling back to simulation.")

        # Option 3: No live Workspace/SMTP — must NOT be reported as a successful send
        logger.warning(
            f"[GMAIL DISCONNECTED] Cannot send email to {to}: Google Workspace not connected "
            f"and SMTP not configured. Returning incomplete (is_mock=True, success=False)."
        )
        return {
            "success": False,
            "message_id": None,
            "to": to,
            "subject": subject,
            "body_preview": body[:120] + "..." if len(body) > 120 else body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": "disconnected",
            "is_mock": True,
            "error": "Google Workspace is not connected; email did not complete.",
            "completed": False,
        }

    # 2. Universal 1-Click Google Calendar Link Generator
    def generate_google_calendar_url(
        self,
        title: str,
        start_time: str,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None
    ) -> str:
        """
        Constructs a universal 1-click 'Add to Google Calendar' URL.
        Allows users to add events directly to their calendar without OAuth login.
        """
        import urllib.parse
        from datetime import timedelta

        def parse_to_compact(dt_str: str, default_add_minutes: int = 60) -> str:
            clean_str = dt_str.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(clean_str)
            except Exception:
                dt = datetime.now(timezone.utc) + timedelta(days=1)
            return dt.strftime("%Y%m%dT%H%M%SZ")

        start_c = parse_to_compact(start_time)
        end_c = parse_to_compact(end_time) if end_time else parse_to_compact(start_time, default_add_minutes=60)

        params = {
            "action": "TEMPLATE",
            "text": title,
            "dates": f"{start_c}/{end_c}",
            "details": description or "Viewing appointment scheduled via Tajlandia.com",
            "location": location or "Thailand"
        }
        return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"

    # 3. Google Calendar Dispatcher
    def create_calendar_event(
        self,
        title: str,
        start_time: str,
        end_time: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        description: Optional[str] = None,
        related_property: Optional[str] = None,
        location: Optional[str] = None,
        staff_user_email: Optional[str] = None,
        db: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Creates event on real Google Calendar if credentials present, and generates 1-click web URL."""
        loc = location or related_property or "Thailand"
        cal_url = self.generate_google_calendar_url(
            title=title,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=loc
        )

        creds = self.get_credentials_for_user(db, staff_user_email)
        if creds:
            try:
                from googleapiclient.discovery import build
                service = build('calendar', 'v3', credentials=creds)

                event_body = {
                    'summary': title,
                    'location': loc,
                    'description': description or f"Viewing appointment for {related_property or 'property'}",
                    'start': {'dateTime': start_time},
                    'end': {'dateTime': end_time or start_time},
                    'attendees': [{'email': e} for e in (attendees or [])]
                }
                created = service.events().insert(calendarId='primary', body=event_body).execute()
                logger.info(f"[PROD CALENDAR] Created event '{title}' via Google Calendar API (ID: {created.get('id')})")
                return {
                    "success": True,
                    "event_id": created.get("id"),
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time or start_time,
                    "attendees": attendees or [],
                    "location": loc,
                    "google_calendar_url": cal_url,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "is_mock": False
                }
            except Exception as exc:
                logger.warning(f"[CALENDAR API NOTICE] Failed to create event via Google Calendar API ({exc}). Falling back to simulation.")

        # No live Calendar credentials — do not report as created
        logger.warning(
            f"[CALENDAR DISCONNECTED] Cannot create event '{title}': Workspace not connected. "
            f"Returning incomplete (is_mock=True, success=False)."
        )
        return {
            "success": False,
            "event_id": None,
            "title": title,
            "start_time": start_time,
            "end_time": end_time or start_time,
            "attendees": attendees or [],
            "location": loc,
            "google_calendar_url": cal_url,
            "description": description or f"Viewing for {related_property or 'Property'}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "is_mock": True,
            "error": "Google Workspace is not connected; calendar event did not complete.",
            "completed": False,
        }


# Singleton manager instance
google_manager = GoogleWorkspaceManager()
