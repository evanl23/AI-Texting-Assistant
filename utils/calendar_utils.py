from datetime import datetime, timedelta
import pytz
import logging
from typing import List, Tuple

from . import time_utils

# These imports are potentially heavy, so we'll import them only when needed
# to avoid slow startup times and circular import issues
_credentials_class = None
_build_function = None

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')
logger = logging.getLogger(__name__)

def _get_google_deps():
    """Lazy import Google API dependencies when needed"""
    global _credentials_class, _build_function
    if _credentials_class is None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        _credentials_class = Credentials
        _build_function = build
    return _credentials_class, _build_function

def list_calendar(creds, day=7) -> List[Tuple[str, str, str]]:
    """List upcoming calendar events"""
    try:
        # Get dependencies
        Credentials, build = _get_google_deps()
        
        now = datetime.now(pytz.UTC).isoformat() 
        credential = Credentials(**creds) # '**' allows dictionary keys to become argument names, and values to be parameter values
        service = build("calendar", "v3", credentials=credential) # Build service to interact with Gcal API
        
        events_result = (
            service.events().list(
                calendarId="primary",
                timeMin=now,
                maxResults=day,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        )
        events = events_result.get("items", [])
        schedule = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            end = event["end"].get("dateTime", event["end"].get("date"))
            schedule.append((start, end, event["summary"]))
        logger.info("Found %d events", len(schedule))
        return schedule
    except Exception as e:
        logger.exception("Failed to list calendar events")
        return []

def add_to_calendar(creds, _event, date, _start, timezone, duration=1, _end=None, frequency=None, byday=None, interval=None) -> int:
    """Add an event to Google Calendar"""
    try:
        # Get dependencies
        Credentials, build = _get_google_deps()
        
        credential = Credentials(**creds)
        service = build("calendar", "v3", credentials=credential)
        
        start = time_utils.standardize_time(date, _start, timezone)
        if type(duration) == type(None):
            duration = 1
        if _end:
            end = time_utils.standardize_time(date, _end, timezone)
        else:
            end = datetime.isoformat(datetime.fromisoformat(start) + timedelta(hours=duration)) # Assume event is 1 hour long
            
        if frequency:
            rule = f'RRULE:FREQ={frequency}'
            if byday:
                rule += f';BYDAY={byday}'
            if interval:
                rule += f';INTERVAL={interval}'
            event = {
                'summary': f'{_event}',
                'start': {
                    'dateTime': f'{start}',
                    'timeZone': 'UTC',
                    },
                'end': {
                    'dateTime': f'{end}',
                    'timeZone': 'UTC',
                    },
                'recurrence': [
                    rule
                    ],
                }
        else:
            event = {
                'summary': f'{_event}',
                'start': {
                    'dateTime': f'{start}',
                    'timeZone': 'UTC',
                    },
                'end': {
                    'dateTime': f'{end}',
                    'timeZone': 'UTC',
                    }
                }
            
        event_result = service.events().insert(calendarId="primary", body=event).execute()
        logger.info("Event added to calendar: %s", event_result.get('htmlLink'))
        return 1
    except Exception as e:
        logger.exception("Failed to add to calendar")
        return 0
