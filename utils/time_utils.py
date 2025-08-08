from datetime import datetime, timedelta
from dateutil.parser import isoparse
import pytz
from typing import Tuple, List

def standardize_time(date_str: str, time_str: str, user_timezone: str = "US/Eastern") -> str:
    """
    Convert date and time strings to a standardized UTC ISO format.
    
    Args:
        date_str (str): Date string in YYYY-MM-DD format
        time_str (str): Time string in HH:MM format
        user_timezone (str): Timezone identifier (default: US/Eastern)
        
    Returns:
        str: UTC datetime in ISO format
    """
    if not user_timezone:
        user_timezone = "US/Eastern"
    if not date_str:  # Check if date is provided, if not, assume today
        date_str = datetime.now(pytz.utc).strftime("%Y-%m-%d")

    # Convert parsed strings to a datetime object
    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

    # Set the correct timezone
    user_tz = pytz.timezone(user_timezone)
    localized_dt = user_tz.localize(naive_dt)

    # Convert to UTC
    utc_dt = localized_dt.astimezone(pytz.utc).isoformat()

    return utc_dt  # Return datetime in UTC

def get_current_utc_time() -> str:
    """
    Get current UTC time with seconds and microseconds zeroed out.
    
    Returns:
        str: Current UTC time in ISO format
    """
    return datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat()

def add_time(time_str: str, *, hours=0, days=0, weeks=0) -> str:
    """
    Add time to a datetime string.
    
    Args:
        time_str (str): ISO format datetime string
        hours (int): Hours to add
        days (int): Days to add
        weeks (int): Weeks to add
        
    Returns:
        str: New datetime in ISO format
    """
    dt = datetime.fromisoformat(time_str)
    new_dt = dt + timedelta(hours=hours, days=days, weeks=weeks)
    return new_dt.isoformat()

def format_time_for_display(iso_time: str, timezone: str ="US/Eastern") -> Tuple[str, str]:
    """
    Format ISO time for display in user's timezone.
    
    Args:
        iso_time (str): ISO format datetime string
        timezone (str): Timezone to display in
        
    Returns:
        tuple: (date_str, time_str) - Formatted date and time for display
    """
    if not user_timezone:
        user_timezone = "US/Eastern"
    dt_obj = datetime.fromisoformat(iso_time)
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=pytz.UTC)
    
    local_dt = dt_obj.astimezone(pytz.timezone(timezone))
    date_str = local_dt.strftime("%Y-%m-%d")
    time_str = local_dt.strftime("%H:%M")
    
    return date_str, time_str

def find_conflict(possible_times: List[str], user_events: List[str], Timezone: str) -> datetime:
    """
    Determines the best time to schedule the event.
    
    Args:
        possible_times (array): an array of ISO format strings for the possible times of the event
        user_events (array[tuples]): an array of tuples of ISO format datetime string where tuple: (start, end, event)
        
    Returns:
        best_time (datetime object): datetime object of the best available time
    """
    user_tz = pytz.timezone(Timezone)
    for time in possible_times:
        time_start = isoparse(time)  # safe for aware datetimes
        if time_start.tzinfo is None: # Ensure time is timezone aware
            time_start = user_tz.localize(time_start)
        time_end = time_start + timedelta(hours=1)

        conflict = False
        for unavailable_start, unavailable_end, event in user_events: # Loop through user schedule to find best time
            event_start = isoparse(unavailable_start)
            event_end = isoparse(unavailable_end)

            # Ensure time is timezone aware
            if event_start.tzinfo is None:
                event_start = user_tz.localize(event_start)
            if event_end.tzinfo is None:
                event_end = user_tz.localize(event_end)

            if time_start < event_end and time_end > event_start:
                conflict = True
                break
        if not conflict:
            return time_start
    return None