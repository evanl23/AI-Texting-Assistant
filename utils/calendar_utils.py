from datetime import datetime, timedelta
import pytz
import logging

# These imports are potentially heavy, so we'll import them only when needed
# to avoid slow startup times and circular import issues
_credentials_class = None
_build_function = None

# Set up logging
logging.basicConfig(level=logging.INFO)
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

def standardize_time(date_str, time_str, user_timezone="US/Eastern"):
    if not date_str: # Check if date is provided, if now, assume today
        date_str = datetime.now(pytz.utc).strftime("%Y-%m-%d")

    # Convert parsed strings to a datetime object
    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

    # Set the correct timezone
    user_tz = pytz.timezone(user_timezone) # Adds a time zone to datetime object
    localized_dt = user_tz.localize(naive_dt)

    # Convert to UTC
    utc_dt = localized_dt.astimezone(pytz.utc).isoformat()

    return utc_dt  # Return datetime in UTC

def list_calendar(creds, day=7):
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
    except Exception as e:
        logger.error(f"Failed to list calendar events: {e}")
        return []

def add_to_calendar(creds, _event, date, _start, timezone, duration=1, _end=None, frequency=None, byday=None, interval=None):
    """Add an event to Google Calendar"""
    try:
        # Get dependencies
        Credentials, build = _get_google_deps()
        
        credential = Credentials(**creds)
        service = build("calendar", "v3", credentials=credential)
        
        start = standardize_time(date, _start, timezone)
        if type(duration) == type(None):
            duration = 1
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
            
        event_result = service.events().insert(calendarId="primary", body=event).execute()
        logger.info(f"Event added to calendar: {event_result.get('htmlLink')}")
        return {"success": True, "link": event_result.get('htmlLink')}
    except Exception as e:
        logger.error(f"Failed to add to calendar: {e}")
        return {"success": False, "error": str(e)}

