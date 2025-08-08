import logging
import base64
from email.message import EmailMessage
from googleapiclient.errors import HttpError
from datetime import datetime
import pytz
import html
from typing import List, Tuple

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')
logger = logging.getLogger(__name__)

_credentials_class = None
_build_function = None
def _get_google_deps():
    """Lazy import Google API dependencies when needed"""
    global _credentials_class, _build_function
    if _credentials_class is None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        _credentials_class = Credentials
        _build_function = build
    return _credentials_class, _build_function

def build_gmail_service(creds):
    try:
        # Get dependencies
        Credentials, build = _get_google_deps()
        credential = Credentials(**creds)
        return build("gmail", "v1", credentials=credential)
    except Exception as e:
        logger.exception("Failed to build Gmail service")

def check_new_emails(service, email_address) -> List[Tuple[str, str, str, str, str, str]]:
    try:
        ten_minutes_ago = int(datetime.now(pytz.UTC).timestamp()) - 600 # Get time in unix time stamp 10 minutes ago

        results = service.users().messages().list(
            userId='me',
            labelIds=["INBOX"],
            q=f'is:unread category:primary after:{ten_minutes_ago}',
            maxResults=5
        ).execute()

        messages = results.get('messages', [])
        emails = []
        logger.info("Found %d unread primary inbox messages for %s", len(emails), email_address)

        for msg in messages:
            msg_id = msg['id']
            msg_detail = service.users().messages().get(userId='me', id=msg_id, format='full').execute() # Get full data

            payload = msg_detail.get('payload', {})
            headers = payload.get('headers', {})

            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
            message_id = next((h["value"] for h in headers if h["name"] == "Message-ID"), None)
            thread_id = msg_detail.get("threadId")
            parts = payload.get('parts', [])
            body = ""

            if parts:
                for part in parts:
                    if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                        data = part['body']['data']
                        body = base64.urlsafe_b64decode(data).decode('utf-8')
                        body = html.unescape(body)
                        break
            else:
                data = payload.get('body', {}).get('data')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8')
                    body = html.unescape(body)

            emails.append((msg_id, body.strip(), sender, subject, message_id, thread_id))
            logger.info("Processed email from %s with subject: %s", sender, subject)

        return emails
    
    except Exception as e:
        logger.exception(f"Error occurred reading emails")
        return []
    
def send_reply(service, reply, recipient, subject, _from, message_id, threadID):
    try:
        message = EmailMessage()
        message.set_content(reply)
        message["To"] = recipient
        message["Subject"] = subject
        message["From"] = _from

        # Set threading headers
        message["In-Reply-To"] = message_id
        message["References"] = message_id

        # encoded message
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

        create_message = {
            "raw": encoded_message,
            "threadId": threadID
        }

        draft = (
            service.users()
            .drafts() # Change to .messages().send() to send message instead of creating draft
            .create(userId="me", body={"message": create_message})
            .execute()
        )
        logger.info('Draft id: %s\nDraft message: %s', draft["id"], draft["message"])
    except HttpError as e:
        logger.warning("HTTP error while creating draft: %s", e)
    except Exception as e:
        logger.exception("Unexpected error while sending reply")