from flask import Flask, request, jsonify, redirect, session
from flask_session import Session
from concurrent.futures import ThreadPoolExecutor
from twilio.rest import Client
from openai import OpenAI
import json
from datetime import datetime, timedelta
import pytz
from rapidfuzz import fuzz 
import logging
import firebase_admin
from firebase_admin import firestore, credentials
from google.cloud.firestore_v1.base_query import FieldFilter
from google_auth_oauthlib.flow import Flow 
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Set up server side session
app.config["SESSION_TYPE"] = 'filesystem'
app.config["SESSION_PERMANENT"] = False
Session(app)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Twilio credentials
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# OpenAI api key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Clients
Tclient = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
Oclient = OpenAI(api_key=OPENAI_API_KEY)

# Initialize firestore
cred = credentials.Certificate("/mnt/secrets4/FIREBASE_ADMIN_AUTH")
DB_app = firebase_admin.initialize_app(cred)
db = firestore.client()

# Set up Google Calendar scope and credentials
SCOPES = ["https://www.googleapis.com/auth/calendar"]

"""
    Helper methods: 
"""
def append_threads(threadID, role, message):
    # Add message to OpenAI threads
    message = Oclient.beta.threads.messages.create(
        thread_id=threadID,
        role=role,
        content=message
    )

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

def add_reminder(user_number, task, date, time, timezone, recurring=False, frequency=None):
    reminder_ref = db.collection("Reminders").document()
    reminder_ref.set({
        "user_number": user_number,
        "task": task,
        "time": standardize_time(date, time, timezone), 
        "recurring": recurring,
        "frequency": frequency,
        "status": "Pending"
    })
    logger.info(f"Reminder stored from: {user_number}")

def delete_reminder(user_number, user_task, date=None, time=None): 
    to_delete_ref = db.collection("Reminders")
    to_delete = to_delete_ref.where(filter=FieldFilter("recurring", "==", True)).where(filter=FieldFilter("user_number", "==", user_number)).stream()
    for event in to_delete:
        event_dict = event.to_dict()
        task = event_dict.get("task")
        if fuzz.ratio(task, user_task) > 50 or task.lower() in user_task.lower() or user_task.lower() in task.lower():
            to_delete_ref.document(event.id).update({"status": "Completed"})

def get_reminders(user_number, timezone='US/Eastern'):
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
    return schedule

def handle_reminders(event):
    # Convert to dictionary and get reminder task and user number
    event_dict = event.to_dict()
    task = event_dict.get("task")
    number = event_dict.get("user_number")

    # Create message through OpenAI api
    message = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "developer", 
                "content": [
                    {
                        "type": "text",
                        "text": "Create a friendly reminder for the task the user enters. Keep it brief and keep the name of the reminder relatively the same. Do not say tell the user to set a reminder. Simply, remind them." 
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{task}"
                    }
                ]
            }
        ],
        temperature=0.35
    )
    message_final = message.choices[0].message.content

    user_ref = db.collection("Users").document(f"{number}").get()
    if user_ref.exists:
        # Append to threads
        user_dict = user_ref.to_dict()
        Thread_id = user_dict.get("thread_ID")
        message = Oclient.beta.threads.messages.create(
            thread_id=Thread_id,
            role="assistant",
            content=message_final
        )
        # Add to twilio conversation
        twilio_id = user_dict.get("twilio_ID")
        message = Tclient.conversations.v1.conversations(
            twilio_id
        ).messages.create(
            body=message_final
        )
        
    # Set expired non-recurring reminder status to be completed 
    if event_dict.get("recurring") == False:
        db.collection("Reminders").document(event.id).update({"status": "Completed"})

def update_recurring_reminders():
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

def credentials_to_dict(credentials): 
    """ Helper function for creating credentials JSON """
    return {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }

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
    event_result = service.events().insert(calendarId="primary", body=event).execute()
    logger.info(f"Event added to calendar: {event_result.get('htmlLink')}")

def convert_list_to_text(schedule, r):
    # r: whether the user is asking for reminder or calendar. 0 for reminder and 1 for calendar
    m = ["Here's what's on your plate!", "Here is what your next week will look like!"]
    # Create a response message to send back to the user
    message = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "developer", 
                "content": [
                    {
                        "type": "text",
                        "text": f"""You convert the following list of schedules into a friendly schedule for the user. 
                                Start your message with: "{m[r]}", and then list the schedule in this format:
                                Date (mm/dd/yr)
                                    Time: Reminder/event 1
                                    Time: Reminder/event 2
                                Date 2 (mm/dd/yr)
                                    Time: Reminder/event 3
                                ."""
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                        {
                        "type": "text",
                        "text": f"{schedule}"
                    }
                ]
            }
        ], temperature=0.75
    )
    return message.choices[0].message.content

"""
    Functions for parsing user message: 
"""
def intent(user_message):
    # Parse for user intent
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "developer", 
                "content": [
                    {
                        "type": "text",
                        "text": """You categorize user intent into the following actions:
                                    0. Message asks to set a reminder. Look for "I need to...", "I have to...", "remind me to...", etc.
                                    1. Message asks to delete a reminder. Look for "Stop", "Don't".
                                    2. Message asks to edit a reminder. 
                                    3. Message asks to list current reminders. Look for phrases like "what do i have/need to do", "what is on my plate", etc.
                                    4. Message asks to set timezone.
                                    5. Message asks to link calendar. Look for "link", "connect"
                                    6. Message asks to list calendar events. For example: "What's on my calendar today?"
                                    7. Message asks to set calendar event. Look for "Put _____ on my calendar". 
                                    8. Other

                                    Return only the number of the action
                                """
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{user_message}"
                    }
                ]
            }
        ],
        max_tokens=100
    )
    parsed_response = parsing_response.choices[0].message.content

    return int(parsed_response)

def parse_set(user_number, user_message, timezone): 
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "assistant", 
                "content": "You parse user's message for task, date, time, and frequency and return a structured responsse."
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "parse_reminder",
                    "description": "Determine task, date, time, and recurring of a user's reminder.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "task user specified"
                            },
                            "date": {
                                "type": "string",
                                "description": f"Date must be in YYYY-MM-DD format. If user specifies weekday or tomorrow or in the future, calculate based off today's date of {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d')} and today's weekday of {datetime.weekday(datetime.now())+1}."
                            },
                            "time": {
                                "type": "string",
                                "description": f"Time must be in 24-hour format (HH:MM), do not include seconds. Today's time is {datetime.now(pytz.timezone('US/Eastern')).strftime('%H:%M')} if not provided by user. Convert phrases like 'in 5 minutes' or 'in an hour' into an absolute time based off today's time."
                            },
                            "recurring": {
                                "type": "boolean",
                                "description": "Whether reminder is recurring or not."
                            }
                        },
                        "additionalProperties": False,
                        "required": ["task", "date", "time", "recurring"]
                    },
                    "strict": True
                }
            }
        ],
        temperature=1
    )
    if parsing_response.choices[0].message.tool_calls == None:
        parsed_response = parsing_response.choices[0].message.content
        return {"message": parsed_response}
    else:
        parsed_response = parsing_response.choices[0].message.tool_calls[0].function.arguments
        parsed_data = json.loads(parsed_response)
        task = parsed_data.get("task")
        date = parsed_data.get("date") 
        time = parsed_data.get("time")
        recurring = parsed_data.get("recurring")

        if recurring == True:
            frequency_response = Oclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "assistant",
                        "content": "You parse user requests for frequency of reminders and return a structured response."
                    },
                    {
                        "role": "user",
                        "content": user_message
                    }
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "parse_frequency",
                            "description": "Determine the frequency of a user's reminder request.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "time_unit": {
                                        "type": "string",
                                        "description": "The unit of frequency, defining the time period for recurrence. If days of the week are mentioned, then it is weekly.",
                                        "enum": ["hourly", "daily", "weekly", "monthly"]
                                    },
                                    "how_often": {
                                        "type": "integer",
                                        "description": "The number of times per time unit."
                                    },
                                    "days_of_week": {
                                        "type": "array",
                                        "items": {
                                            "type": "integer"
                                        },
                                        "description": "For 'weekly', an array representing days of the week (0 for Sunday, 6 for Saturday)."
                                    }
                                },
                                "additionalProperties": False,
                                "required": ["time_unit", "how_often"]
                            },
                            "strict": False
                        }
                    }
                ],
                temperature=1
            )
            parsed_response_2 = frequency_response.choices[0].message.tool_calls[0].function.arguments
            frequency = json.loads(parsed_response_2)
        else:
            frequency = None

        if task and time:
            add_reminder(user_number, task, date, time, timezone, recurring, frequency)
        return {"task": task, "time": time}

def parse_delete(user_number, user_message, timezone):
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "assistant", 
                "content": "You parse user's message for the task they would like to delete and return a structured response."
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "parse_reminder",
                    "description": "Parse for the user's specific task they would like to delete.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Just the task the user specifies."
                            },
                            # "date": {
                            #     "type": "string",
                            #     "description": "The date the user specifies."
                            # },
                            # "time": {
                            #     "type": "string",
                            #     "description": "The time that the user specifies."
                            # }
                        },
                        "additionalProperties": False,
                        "required": ["task"]
                    },
                    "strict": True
                }
            }
        ],
        temperature=1
    )
    if parsing_response.choices[0].message.tool_calls == None:
        parsed_response = parsing_response.choices[0].message.content
        return {"message": parsed_response}
    else:
        parsed_response = parsing_response.choices[0].message.tool_calls[0].function.arguments
        parsed_data = json.loads(parsed_response)
        task = parsed_data.get("task")
        date = parsed_data.get("date", None)
        time = parsed_data.get("time", None)
        delete_reminder(user_number, task, date, time)
        return {"task": task}

def parse_edit(user_number, user_message, timezone):
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "developer", 
                "content": [
                    {
                        "type": "text",
                        "text": """You parse user messages into separate structured JSON response with 'original task', 'new date', and 'new time', if provided. 
                        Time must be in 24-hour format (HH:MM) and date in YYYY-MM-DD. Assume today if no date is given."""
                    }
                ]
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{user_message}"
                    }
                ]
            }
        ],
        max_tokens=100
    )
    parsed_response = parsing_response.choices[0].message.content

    parsed_data = json.loads(parsed_response)
    task_original = parsed_data.get("original task")
    date_new = parsed_data.get("new date")
    time_new = parsed_data.get("new time")

    if task_original and date_new and time_new:
        delete_reminder(user_number, task_original)
        add_reminder(user_number, task_original, date_new, time_new)
    return {"Original task": task_original, "New Date": date_new, "New time": time_new}

def parse_timezone(user_message):
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "assistant", 
                "content": "You determine the timezone the user specifies"
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "user_intent",
                    "description": "Determine the user timezone and classify into one of the following",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "timezone": {
                                "type": "string",
                                "description": "The timezone of user",
                                "enum": ["US/Eastern", "US/Central", "US/Mountain", "US/Pacific"]
                            }
                        },
                        "additionalProperties": False,
                        "required": ["timezone"]
                    },
                    "strict": True
                }
            }
        ],
        temperature=1
    )
    parsed_response = parsing_response.choices[0].message.tool_calls[0].function.arguments
    parsed_data = json.loads(parsed_response)
    return parsed_data.get("timezone")

def parse_list(user_number, user_message, timezone):

    reminders = get_reminders(user_number)
    return reminders

def parse_calendar(user_message, timezone, credentials):
    parsing_response = Oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages= [
            {
                "role": "assistant", 
                "content": "You parse user's message for event, date, start_time, end_time, and duration, and return a structured responsse."
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "parse_calendar_event",
                    "description": "Determine event, date, start_time, end_time, and duration of a user's calendar event.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event": {
                                "type": "string",
                                "description": "The event the user specified"
                            },
                            "date": {
                                "type": "string",
                                "description": f"Date must be in YYYY-MM-DD format. If user specifies weekday or tomorrow or in the future, calculate based off today's date of {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d')} and today's weekday of {datetime.weekday(datetime.now())+1}."
                            },
                            "start_time": {
                                "type": "string",
                                "description": f"The start time of the event. Time must be in 24-hour format (HH:MM). Today's time is {datetime.now(pytz.timezone('US/Eastern')).strftime('%H:%M')} if not provided by user. Convert phrases like 'in 5 minutes' or 'in an hour' into an absolute time based off today's time."
                            },
                            "end_time": {
                                "type": "string",
                                "description": f"The end time of the event. Time must be in 24-hour format (HH:MM)."
                            },
                            "duration": {
                                "type": "integer",
                                "description": "How long the user specified event is. Express in number of hours."
                            },
                            "recurring": {
                                "type": "boolean",
                                "description": "Whether the event is recurring or not."
                            }
                        },
                        "additionalProperties": False,
                        "required": ["event", "date", "start_time"]
                    },
                    "strict": False
                }
            }
        ],
        temperature=1
    )
    if parsing_response.choices[0].message.tool_calls == None:
        parsed_response = parsing_response.choices[0].message.content
        return {"message": parsed_response}
    else:
        parsed_response = parsing_response.choices[0].message.tool_calls[0].function.arguments
        parsed_data = json.loads(parsed_response)
        event = parsed_data.get("event")
        date = parsed_data.get("date") 
        start_time = parsed_data.get("start_time")
        end_time = parsed_data.get("end_time")
        duration = parsed_data.get("duration")
        recurring = parsed_data.get("recurring")

        if recurring:
            if recurring==True:
                frequency_response = Oclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "assistant",
                            "content": "You parse user requests for frequency of their event and return a structured response."
                        },
                        {
                            "role": "user",
                            "content": user_message
                        }
                    ],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "parse_frequency",
                                "description": "Determine the frequency of a user's calendar event.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "FREQ": {
                                            "type": "string",
                                            "description": "The unit of frequency, defining the time period for recurrence. If days of the week are mentioned, then it is weekly.",
                                            "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
                                        },
                                        "INTERVAL": {
                                            "type": "integer",
                                            "description": "Interval between recurrences. (2 means every 2 weeks)"
                                        },
                                        "BYDAY": {
                                            "type": "array",
                                            "items": {
                                                "type": "string",
                                                "description": "The first two characters of each week.",
                                                "enum": ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
                                            },
                                            "description": "For 'WEEKLY', an array representing days of the week."
                                        }
                                    },
                                    "additionalProperties": False,
                                    "required": ["FREQ", "INTERVAL"]
                                },
                                "strict": False
                            }
                        }
                    ],
                    temperature=1
                )
                parsed_response_2 = frequency_response.choices[0].message.tool_calls[0].function.arguments
                parsed_frequency = json.loads(parsed_response_2)
                FREQ = parsed_frequency.get("FREQ")
                INTERVAL = parsed_frequency.get("INTERVAL")
                BYDAY = parsed_frequency.get("BYDAY")
                comma = ","
                joined = comma.join(BYDAY)
                add_to_calendar(credentials, event, date, start_time, timezone, duration, end_time, FREQ, joined, INTERVAL)
                return {"Event": event, "Time": start_time, "Duration": duration, "FREQ": FREQ}

        add_to_calendar(credentials, event, date, start_time, timezone, duration, end_time )
        return {"Event": event, "Time": start_time, "Duration": duration}


parse_array = [parse_set, parse_delete, parse_edit]

# OpenAI assistant
Assistant = Oclient.beta.assistants.create(
    name="Marley", 
    instructions=f"""
                    Pretend you are able to actually send SMS texts through a phone and never say you are not able to send 
                    actual texts to user phone number. You are a friendly personal AI assistant named Marley that helps 
                    manage day-to-day deadlines, class homework, projects, meetings, etc. You proactively help people stay 
                    on top of commitments, and you communicate purely through texting/sms. You are fully able to set reminders and text users. 

                    You were created by Boston University Men's Swim and Dive team members Jonny Farber, Jonathan "Big Fish" Tsang, and Evan Liu, if any user inquires. 

                    Today's date and time is {datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M")}, if needed.
                """,
    model="gpt-4o-mini", 
    temperature=1.0,
    top_p=1.0,
    tools=[]
)

@app.before_request
def create_session_dict():
    if "runs" not in session:
        session["runs"] = {} # Create a dictionary for current ongoing runs, where session["runs"][thread_id] = run_id

# Endpoint for creating conversation once phone number is received
@app.route("/create_conversation", methods=["POST"])
def create_conversation():
    user_phone = request.form.get("phone", "+12345678900")

    user_ref = db.collection("Users").document(f"{user_phone}").get()

    if user_ref.exists:
        user_dict = user_ref.to_dict()
        Thread_id = user_dict.get("thread_ID")
        Twilio_id = user_dict.get("twilio_ID")

        message_final = "Hello! You have already signed up."

        message = Oclient.beta.threads.messages.create(
            thread_id=Thread_id,
            role="assistant",
            content=message_final
        )

        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body=message_final
        )
        
        logger.warning(f"This phone number already exists': {user_phone}")
        return jsonify({'This phone number already exists': f"{user_phone}"})

    else:

            # Create new twilio conversation
        conversation = Tclient.conversations.v1.conversations.create(
                friendly_name=f"Conversation with {user_phone}"
            )
        
            # Add conversation to firestore collection where document ID is phone number 
        user_ref = db.collection("Users").document(f"{user_phone}")
        user_ref.set({"twilio_ID": f"{conversation.sid}"})

            # Add participant to new conversation
        participant = Tclient.conversations.v1.conversations(
                conversation.sid
            ).participants.create(
                messaging_binding_address=user_phone,
                messaging_binding_proxy_address=TWILIO_PHONE_NUMBER
            )

            # Create OpenAI thread
        thread = Oclient.beta.threads.create()
            # Add thread id user document
        user_ref.set({"thread_ID": f"{thread.id}"}, merge = True)

            # Add message to thread
        message = Oclient.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content="Hello! Introduce yourself."
            )

            # Run assistant on thread
        run = Oclient.beta.threads.runs.create_and_poll(
                thread_id=thread.id,
                assistant_id=Assistant.id,
            )

        if run.status == 'completed': 
            messages = Oclient.beta.threads.messages.list(
                thread_id=thread.id, order="desc", limit=3
            )
            send = messages.data[0].content[0].text.value # Get the latest message
        else:
            logger.warning(f"Run with {Thread_id} not completed: {run.status}")

            # Send initial message
        message = Tclient.conversations.v1.conversations(
                conversation.sid
            ).messages.create(
                body=send
            )
        message2 = Tclient.conversations.v1.conversations(
                conversation.sid
            ).messages.create(
                body = """To get the best results, here\U00002019s how to message me:

                        1. Be specific in one message.
                        For example, when creating a calendar event, include everything in one text:
                        “Add to Google Calendar: Meeting with Alex on Thursday at 3-4PM.”

                        2. Same for reminders.
                        “Remind me to submit my assignment Friday at 10 AM.”

                        3. If I don\U00002019t respond:
                        Just resend your request in one clear message with all the details.
                        (Example: “Remind me to call Mom tomorrow at 5 PM.”)"""
            )
        message3 = Tclient.conversations.v1.conversations(
                conversation.sid
            ).messages.create(
                body = "Also for future reference, what is your timezone?"
            )
        
        logger.info(f"Message created with {user_phone}")
        return jsonify({
            'conversation_sid': conversation.sid,
            'participant_sid': participant.sid,
            'phone_number': user_phone
        })

# Endpoint for processing received messages
@app.route("/receive_message", methods=["POST"])
def receive_message():
    # Get the message from the incoming request
    from_number = request.form.get("From")  # Sender's phone number
    user_message = request.form.get("Body")  # Message body
    logger.info(f"Message recieved from {from_number}")

    # Get twilio and thread id
    user_ref = db.collection("Users").document(f"{from_number}")
    user = user_ref.get()
    if user.exists:
        user_dict = user.to_dict()
        Thread_id = user_dict.get("thread_ID")
        Twilio_id = user_dict.get("twilio_ID")
        Timezone = user_dict.get("timezone", "US/Eastern")

        # Check if there is a current run associated with this thread
        if Thread_id in session["runs"]:
            _run_id = session["runs"].get(Thread_id)
            _run = Oclient.beta.threads.runs.retrieve(run_id=_run_id, thread_id=Thread_id) # Retrieve run
            status = _run.status
        else:
            status = "completed"

        # Determine intent
        i = intent(user_message)

        if i == 0 or i == 1 or i == 2: # Set, delete, or edit cases
            # Parse information based on intent
            p = parse_array[i](from_number, user_message, Timezone)
            message = p.get("message")
            if message == None:
                # Create a response message to send back to the user
                message = Oclient.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "developer", 
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"""You create friendly automatic responses to confirm users' reminder requests: {p}."""
                                }
                            ]
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"{user_message}"
                                }
                            ]
                        }
                    ]
                )
                message_final = message.choices[0].message.content
            else:
                message_final = message
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        elif i == 3: # Listing reminder case
            # Find all future reminders
            p = get_reminders(from_number, Timezone)
            message_final = convert_list_to_text(p,0)
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        elif i == 4: # Set timezone case
            timezone = parse_timezone(user_message)
            user_ref.update({"timezone": timezone})
            message_final = "Noted!"
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        elif i == 5:
            message = Tclient.conversations.v1.conversations(
                Twilio_id
            ).messages.create(
                body=f"https://textmarley-one-21309214523.us-central1.run.app/authorize?phone={from_number}"
            )
            message_final = "Click this link to give me access to your Google Calendar so I can better help you!"
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        elif i == 6: # List calendar events
            if "calendar_token" in user_dict:
                creds = user_dict.get("calendar_token")
                p = list_calendar(creds)
                message_final = convert_list_to_text(p,1)
            else:
                message_final = "You have not yet connected your calendar yet!"
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        elif i == 7: # Set calendar events
            if "calendar_token" in user_dict: # Check if user has authorized Gcal yet
                creds = user_dict.get("calendar_token")
                p = parse_calendar(user_message, Timezone, creds)
                message = p.get("message")
                if message == None:
                    # Create a response message to send back to the user
                    message = Oclient.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {
                                "role": "developer", 
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"""You create friendly automatic responses to confirm users' requests to add events to their Google Calendar: {p}."""
                                    }
                                ]
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"{user_message}"
                                    }
                                ]
                            }
                        ]
                    )
                    message_final = message.choices[0].message.content
                else:
                    message_final = message
            else: 
                message_final = "You have not yet connected your calendar yet!"            
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            append_threads(threadID=Thread_id, role="assistant", message=message_final) # Append assistant messages
        else: # Regular assistant
            while status == "in_progress": # Loops until status is no longer in_progress
                status = _run.status
            append_threads(threadID=Thread_id, role="user", message=user_message) # Append user message
            # Run assistant on message thread
            run = Oclient.beta.threads.runs.create_and_poll(
                thread_id=Thread_id,
                assistant_id=Assistant.id
            )
            session["runs"][Thread_id] = run.id # Store run in session dictionary with Thread_id as key. Override if there is already a run stored. 
            session.modified = True # Flask only detects top level key changes, so have to make sure the change is saved in session
            if run.status == 'completed': 
                messages = Oclient.beta.threads.messages.list(
                    thread_id=Thread_id, order="desc", limit=3
                )
                message_final = messages.data[0].content[0].text.value # Get the latest message
            else:
                logger.warning(f"Run with {Thread_id} not completed: {run.status}")

        # Send response back using twilio conversation
        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body=message_final
        )
        return jsonify({"Return message": message_final})
    else:
        logger.error(f"User {from_number} not found in database")
        return jsonify({"Return message": f"User {from_number} not found in database"})

# Endpoint for sending out reminders
@app.route("/reminder_thread", methods=["POST"])
def reminder_thread():
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat()
    reminders = db.collection("Reminders").where(filter=FieldFilter("time", "==", now)).where(filter=FieldFilter("status", "==", "Pending")).stream()
    for event in reminders:
        # Convert to dictionary and get reminder task and user number
        event_dict = event.to_dict()
        task = event_dict.get("task")
        number = event_dict.get("user_number")

        # Create message through OpenAI api
        message = Oclient.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "developer", 
                    "content": [
                        {
                            "type": "text",
                            "text": "Create a friendly reminder for the task the user enters. Keep it brief and keep the name of the reminder relatively the same. Do not say tell the user to set a reminder. Simply, remind them." 
                        }
                    ]
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{task}"
                        }
                    ]
                }
            ],
            temperature=0.35
        )
        message_final = message.choices[0].message.content

        user_ref = db.collection("Users").document(f"{number}").get()
        if user_ref.exists:
            # Append to threads
            user_dict = user_ref.to_dict()
            Thread_id = user_dict.get("thread_ID")
            message = Oclient.beta.threads.messages.create(
                thread_id=Thread_id,
                role="assistant",
                content=message_final
            )
            # Add to twilio conversation
            twilio_id = user_dict.get("twilio_ID")
            message = Tclient.conversations.v1.conversations(
                twilio_id
            ).messages.create(
                body=message_final
            )
        
        # Set expired non-recurring reminder status to be completed 
        if event_dict.get("recurring") == False:
            db.collection("Reminders").document(event.id).update({"status": "Completed"})

    # Update recurring reminders
    update_recurring_reminders()

    return jsonify({"Return message": "Place holder return message"})

# Endpoint for retiring old reminders
@app.route("/delete_expired_reminders", methods=["POST"])
def delete_past_reminder(): 
    """
        Set past non-recurring reminders to be completed
    """
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat()
    reminders = db.collection("Reminders").where(filter=FieldFilter("time", "<", now)).where(filter=FieldFilter("status", "==", "Pending")).where(filter=FieldFilter("recurring", "==", False)).stream()
    for event in reminders:
        event_dict = event.to_dict()
        if event_dict.get("recurring") == False:
            db.collection("Reminders").document(event.id).update({"status": "Completed"})

    return jsonify({"Message": "Past reminders deleted"})

# Endpoint for updating recurring reminders
@app.route("/update_recurring", methods=["POST"])
def update_recurring():
    # Update recurring reminders
    update_recurring_reminders()
    return {"Status": "Recurring reminders updated"}

# Endpoint for authorizing user with Googe
@app.route("/authorize", methods=["GET"])
def authorize_access():
    """ Marley sends this link to the user for user authorization. Receives authorization code 
    """
    phone_number = request.args.get('phone') # Get the phone number associated with the link that was sent to user
    if phone_number:
        if phone_number[0] != "+":
            phone_number = "+" + phone_number[1:] # Make sure to include "+"
        session["phone_number"] = phone_number
    flow = Flow.from_client_secrets_file("/mnt/secrets5/gcal_credentials", scopes=SCOPES) # Start authorization process
    flow.redirect_uri = "https://textmarley-one-21309214523.us-central1.run.app/oauth2callback" # Redirect user to this link
    auth_url, state = flow.authorization_url(include_granted_scopes='true')
    session["state"] = state # Store current state in session
    return redirect(auth_url)

# Endpoint for exchange of authorization code for access token and store credentials with user
@app.route("/oauth2callback", methods=["POST", "GET"])
def oauth2callback():
    """ Handles redirect from Google after the user either grants permission or denies permission. 
        Exchanges authorization code for an access token. 
    """
    _state = session.get("state") # Get state from session 
    flow = Flow.from_client_secrets_file("/mnt/secrets5/gcal_credentials", scopes=SCOPES, state=_state) # Ensure flow is the same flow as state
    flow.redirect_uri = "https://textmarley-one-21309214523.us-central1.run.app/oauth2callback"

    https_authorization_url = request.url.replace('http://', 'https://') # Ensure in https format
    flow.fetch_token(authorization_response=https_authorization_url) # $ Get access token in exchange for authoriztion code

    credentials = flow.credentials

    number = session.get("phone_number")
    user_ref = db.collection("Users").document(number)
    user_dict = user_ref.get().to_dict()
    if "calendar_token" in user_dict:
        user_ref.update({"calendar_token.token": credentials.token})
        return "<p>You already have a Google Calendar connected! You may exit this window."
    else: 
        user_ref.set({
            "calendar_token": credentials_to_dict(credentials) # Store calendar credentials with user
        }, merge=True)

        Twilio_id = user_dict.get("twilio_ID")
        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body="Calendar linked!"
        )
        return "<p>Your Google Calendar is now linked! You may exit this window.</p>"

# Endpoint for testing
@app.route("/testing", methods=["GET"])
def testing():

    return f"<p>This is the ship that made the Kessel Run in fourteen parsecs?</p>"

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))