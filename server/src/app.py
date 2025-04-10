from flask import Flask, request, jsonify
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
import os

app = Flask(__name__)

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

# Parse for user intent
def intent(user_message):
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
                                    4. Message asks to set timezone
                                    5. Other

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

def standardize_time(date_str, time_str, user_timezone="US/Eastern"):
    if not date_str: # Check if date is provided, if now, assume today
        date_str = datetime.now(pytz.utc).strftime("%Y-%m-%d")

    # Convert parsed strings to a datetime object
    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

    # Set the correct timezone
    user_tz = pytz.timezone(user_timezone)
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

def delete_reminder(user_number, user_task, date=None, time=None): 
    to_delete_ref = db.collection("Reminders")
    to_delete = to_delete_ref.where(filter=FieldFilter("recurring", "==", True)).where(filter=FieldFilter("user_number", "==", user_number)).stream()
    for event in to_delete:
        event_dict = event.to_dict()
        task = event_dict.get("task")
        if fuzz.ratio(task, user_task) > 50 or task.lower() in user_task.lower() or user_task.lower() in task.lower():
            to_delete_ref.document(event.id).update({"status": "Completed"})

def get_reminders(user_number, timezone='US/Eastern'):
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat() # Don't change all times in database is utc
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

def update_recurring_reminders():
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat() # Don't change all times in database is utc
    # Get all reminders that are recurring and before now
    reminders = db.collection("Reminders").where(filter=FieldFilter("recurring", "==", True)).where(filter=FieldFilter("time", "<", now)).stream() # Returns a stream of documents

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

# Functions for parsing user message
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
                                "description": f"Time must be in 24-hour format (HH:MM). Today's time is {datetime.now(pytz.timezone('US/Eastern')).strftime('%H:%M')} if not provided by user. Convert phrases like 'in 5 minutes' or 'in an hour' into an absolute time based off today's time."
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
                "content": "You parse user's message for task, date, and time, and return a structured response."
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
                    "description": "The user wishes to delete a reminder, determine the task, date, and time if provided.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Just the task the user specifies."
                            },
                            "date": {
                                "type": "string",
                                "description": "The date the user specifies."
                            },
                            "time": {
                                "type": "string",
                                "description": "The time that the user specifies."
                            }
                        },
                        "additionalProperties": False,
                        "required": ["task", "date", "time"]
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
    # parsing_response = Oclient.chat.completions.create(
    #     model="gpt-4o-mini",
    #     messages= [
    #         {
    #             "role": "developer", 
    #             "content": [
    #                 {
    #                     "type": "text",
    #                     "text": """You parse user messages into separate structured JSON response with 'time frame' and 'date', if provided. 
    #                                 If not provided, assume 'date' is today and 'time frame' is null"""
    #                 }
    #             ]
    #         },
    #         {
    #             "role": "user",
    #             "content": [
    #                 {
    #                     "type": "text",
    #                     "text": f"{user_message}"
    #                 }
    #             ]
    #         }
    #     ],
    #     max_tokens=100
    # )
    # parsed_response = parsing_response.choices[0].message.content

    # parsed_data = json.loads(parsed_response)
    # date = parsed_data["date"]
    # time_frame = parsed_data["time frame"]

    reminders = get_reminders(user_number)
    return reminders

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
                print(run.status)

            # Send initial message
        message = Tclient.conversations.v1.conversations(
                conversation.sid
            ).messages.create(
                body=send
            )
        message2 = Tclient.conversations.v1.conversations(
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
    
        # Add message to OpenAI threads
        message = Oclient.beta.threads.messages.create(
            thread_id=Thread_id,
            role="user",
            content=user_message
        )

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
                # Append to threads
            else:
                message_final = message
            message = Oclient.beta.threads.messages.create(
                thread_id=Thread_id,
                role="assistant",
                content=message_final
            )
        elif i == 3: # Listing reminder case
            # Find all future reminders
            p = get_reminders(from_number, Timezone)

            # Create a response message to send back to the user
            message = Oclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "developer", 
                        "content": [
                            {
                                "type": "text",
                                "text": """You convert the following list of schedules into a friendly schedule for the user. 
                                        Start your message with: "Here's what's on your plate!", and then list the schedule in this format:
                                        Date
                                            Time: Reminder 1
                                            Time: Reminder 2
                                        Date 2
                                            Time: Reminder 3
                                        ."""
                            }
                        ]
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"{p}"
                            }
                        ]
                    }
                ], temperature=0.75
            )
            message_final = message.choices[0].message.content
            # Append to threads
            message = Oclient.beta.threads.messages.create(
                thread_id=Thread_id,
                role="assistant",
                content=message_final
            )
        elif i == 4: # Set timezone case
            timezone = parse_timezone(user_message)
            user_ref.update({"timezone": timezone})
            message_final = "Noted!"
        else:
            # Run assistant on message thread
            run = Oclient.beta.threads.runs.create_and_poll(
                thread_id=Thread_id,
                assistant_id=Assistant.id
            )
            if run.status == 'completed': 
                messages = Oclient.beta.threads.messages.list(
                    thread_id=Thread_id, order="desc", limit=3
                )
                message_final = messages.data[0].content[0].text.value # Get the latest message
            else:
                print(run.status)

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

        # lol
        jo = "jerk off"
        j = "jerk"
        if jo in task or j in task:
            message_final += "\U0001F609 \U0001F609 \U0001F4A6 \U0001F4A6"

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

@app.route("/morning_to_dos", methods=["POST"])
def morning():
    # Get all users that want to recieve message in the morning
    user_ref = db.collection("Users").where(filter=FieldFilter("morning", "==", True)).stream()
    for user in user_ref:
        user_dict = user.to_dict()
        twilio_id = user_dict.get("twilio_ID")
        user_number = user.id

        get_reminders(user_number)

        # Send reminders

    return {"Status": "Morning message sent"}

@app.route("/testing", methods=["GET"])
def testing():
    # convo_ref = db.collection("Conversations").stream()
    
    # for convo in convo_ref:
    #     convo_dict = convo.to_dict()
    #     convo_ID = convo_dict.get("twilio_ID")
    #     thread_ID = convo_dict.get("thread_ID")

    #     Users_ref = db.collection("Users").document(convo.id)
    #     Users_ref.set({"twilio_ID": convo_ID, "thread_ID": thread_ID})
    
    # convo_ref = db.collection("Testing").stream()
    # for convo in convo_ref:
    #     db.collection("Testing").document(convo.id).set({"Hello": "World"}, merge = True)

    return f"<p>This is the ship that made the Kessel Run in fourteen parsecs?: </p>"

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
