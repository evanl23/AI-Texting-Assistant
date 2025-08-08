import logging
from google.cloud.firestore_v1.base_query import FieldFilter
from rapidfuzz import fuzz 
from datetime import datetime, timedelta
import pytz
from openai import OpenAI
from twilio.rest import Client
import os
from typing import List, Tuple

from . import time_utils

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')
logger = logging.getLogger(__name__)

_oclient = None
_tclient = None

def get_openai_client():
    """Initialize and return the OpenAI client"""
    global _oclient
    if _oclient is None:
        # Api key
        openai_api_key = os.getenv("OPENAI_API_KEY")
        _oclient = OpenAI(api_key=openai_api_key)
    return _oclient

def get_twilio_client():
    """Initialize and return the Twilio client"""
    global _tclient
    if _tclient is None:
        # Api keys
        twilio_sid = os.getenv("TWILIO_SID")
        twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        _tclient = Client(twilio_sid, twilio_auth_token)
    return _tclient

def add_reminder(user_number, db, task, date, time, timezone, recurring=False, frequency=None) -> int:
    reminder_ref = db.collection("Reminders").document()
    try:
        reminder_ref.set({
            "user_number": user_number,
            "task": task,
            "time": time_utils.standardize_time(date, time, timezone), 
            "recurring": recurring,
            "frequency": frequency,
            "status": "Pending"
        })
        logger.info("Added reminder for %s: %s at %s", user_number, task, time)
        return 1
    except Exception as e:
        logger.exception("Failed to add reminder for %s", user_number)
        return 0

def delete_reminder(user_number, db, user_task, date=None, time=None): 
    logger.info("Attempting to mark recurring reminder for %s matching task: %s as completed", user_number, user_task)
    to_delete_ref = db.collection("Reminders")
    to_delete = to_delete_ref.where(filter=FieldFilter("recurring", "==", True)).where(filter=FieldFilter("user_number", "==", user_number)).stream()
    for event in to_delete:
        event_dict = event.to_dict()
        task = event_dict.get("task")
        if fuzz.ratio(task, user_task) > 50 or task.lower() in user_task.lower() or user_task.lower() in task.lower():
            logger.info("Setting %s to be completed", task)
            to_delete_ref.document(event.id).update({"status": "Completed"})

def get_reminders(user_number, db, timezone='US/Eastern') -> List[Tuple[str, str]]:
    if not timezone:
        timezone = "US/Eastern"
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat() # Don't change. All times in database is utc
    reminders = db.collection("Reminders").where(filter=FieldFilter("user_number", "==", user_number)).where(filter=FieldFilter("time", ">=", now)).where(filter=FieldFilter("status", "==", "Pending")).order_by("time").stream()

    schedule = []
    for reminder in reminders:
        d = reminder.to_dict()
        task = d.get("task")
        time = d.get("time")

        # Convert to datetime object
        dt_obj = datetime.fromisoformat(time)
        if dt_obj.tzinfo is None:
            # Add timezone
            dt_obj = dt_obj.replace(tzinfo=pytz.timezone(timezone))

        # Convert to user timezone
        dt = dt_obj.astimezone(pytz.timezone(timezone)).isoformat()

        schedule.append((task, dt))
    logger.info("Found %d reminders associated with user: %s", len(schedule), user_number)
    return schedule

def handle_reminders(event, db):
    # Convert to dictionary and get reminder task and user number
    event_dict = event.to_dict()
    task = event_dict.get("task")
    number = event_dict.get("user_number")

    # Get OpenAI client and create message
    try:
        Oclient = get_openai_client()
        message = Oclient.responses.create(
            model="gpt-4o-mini",
            instructions= "Create a friendly reminder for the task the user enters. Keep it brief and keep the name of the reminder relatively the same. Do not say tell the user to set a reminder. Simply, remind them.",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"{task}"
                        }
                    ]
                }
            ],
            temperature=0.35
        )
        message_final = message.output_text
    except Exception as e:
        logger.exception("Failed to generate reminder with OpenAI for user %s", number)
        return
    
    user_ref = db.collection("Users").document(f"{number}").get()
    if user_ref.exists:
        user_dict = user_ref.to_dict()
        # Add to twilio conversation
        twilio_id = user_dict.get("twilio_ID")
        try:
            Tclient = get_twilio_client()
            message = Tclient.conversations.v1.conversations(
                twilio_id
            ).messages.create(
                body=message_final
            )
        except Exception as e:
            logger.exception("Failed to send message to %s via Twilio", number)
    else:
        logger.warning("Failed to find Firestore document for user %s", number)
        
    # Set expired non-recurring reminder status to be completed 
    if event_dict.get("recurring") == False:
        db.collection("Reminders").document(event.id).update({"status": "Completed"})

def update_recurring_reminders(db):
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat() # Don't change all times in database is utc
    # Get all reminders that are recurring and before now
    reminders = db.collection("Reminders").where(filter=FieldFilter("status", "==", "Pending")).where(filter=FieldFilter("recurring", "==", True)).where(filter=FieldFilter("time", "<", now)).stream() # Returns a stream of documents

    # Iterate through all reminders
    for event in reminders: # Reads document in document stream that match the query
        event_dict = event.to_dict()
        time = event_dict.get("time")
        frequency = event_dict.get("frequency")
        
        # Get frequency specifics
        time_unit = frequency.get("time_unit")
        how_often = frequency.get("how_often")
        days_of_week = frequency.get("days_of_week", None)

        if time_unit == 'hourly':
            time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(hours=how_often))
        elif time_unit == "daily":
            time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(days=how_often))
        elif time_unit == "weekly":
            # If there are no days of the week, then it is every {how_often} weeks
            if days_of_week is None: 
                time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(weeks=how_often))
            # If only one day of the week or all days of the week, then it is every one week
            elif len(days_of_week) == 1 or len(days_of_week) == 7: 
                time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(weeks=1))
            else:
                weekday = datetime.weekday(datetime.fromisoformat(now)) + 1 # Datetime has Monday as 0, so need to add 1 to get Monday=1
                if weekday == 7: weekday=0 # Set Sunday to 0
                # Calculate how many days to increment based on today's weekday and {days_of_week}
                difference = days_of_week[0]-weekday+7
                for w in days_of_week:
                    if w > weekday:
                        difference = w-weekday
                        break
                time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(days=difference))
        elif time_unit == "monthly":
            time_new = datetime.isoformat(datetime.fromisoformat(time) + timedelta(weeks=4*how_often))

        # Set new time
        db.collection("Reminders").document(event.id).update({"time": time_new})
