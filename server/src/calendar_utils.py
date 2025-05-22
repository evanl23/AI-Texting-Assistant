from datetime import datetime, timedelta
import pytz
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import logging

from app import standardize_time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def list_calendar(creds, day=7):
    now = datetime.now(pytz.UTC).isoformat() 
    credential = Credentials(**creds) # '**' allows dictionary keys to become argument names, and values to be parameter values
    service = build("calendar", "v3", credentials=credential) # Build service to interact with Gcal API
    events_result = (
        service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=day,
            singleEvents=False,
            orderBy="startTime",
        ).execute()
    )
    events = events_result.get("items", [])
    schedule = []
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        schedule.append((start, event["summary"]))
    return schedule

def add_to_calendar(creds, _event, date, _start, timezone, duration=1, _end=None, frequency=None, byday=None, interval=None):
    credential = Credentials(**creds)
    service = build("calendar", "v3", credentials=credential)
    start = standardize_time(date, _start, timezone)
    if _end:
        end = standardize_time(date, _end, timezone)
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
    try:
        event_result = service.events().insert(calendarId="primary", body=event).execute()
        logger.info(f"Event added to calendar: {event_result.get('htmlLink')}")
    except Exception as e:
        logger.error(f"Failed to add to calendar: {e}")