"""
Email Agent — Gmail API (OAuth, free, uses your own Gmail account, no per-email cost).
Replaces the paid SendGrid option from the original design.
"""
import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import config

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def get_gmail_service():
    """
    First run opens a browser for OAuth consent (needs GMAIL_CREDENTIALS_JSON,
    downloaded free from Google Cloud Console -> OAuth client, Desktop app type).
    Token is cached to GMAIL_TOKEN_JSON afterward — no repeated login.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(config.GMAIL_TOKEN_JSON):
        creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_JSON, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(config.GMAIL_CREDENTIALS_JSON, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(config.GMAIL_TOKEN_JSON) or ".", exist_ok=True)
        with open(config.GMAIL_TOKEN_JSON, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_application_email(to_email: str, subject: str, body: str, cv_path: str) -> dict:
    """Respects the global DRY_RUN flag — logs intent instead of sending when true."""
    if config.DRY_RUN:
        return {
            "dry_run": True,
            "action": "send_application_email",
            "to": to_email,
            "subject": subject,
            "cv_path": cv_path,
        }

    service = get_gmail_service()
    message = MIMEMultipart()
    message["to"] = to_email
    message["subject"] = subject
    message.attach(MIMEText(body))

    with open(cv_path, "rb") as f:
        part = MIMEApplication(f.read(), Name="CV.docx")
    part["Content-Disposition"] = 'attachment; filename="CV.docx"'
    message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"dry_run": False, "gmail_message_id": result.get("id")}
