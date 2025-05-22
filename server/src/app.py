from flask import Flask, request, jsonify, redirect, session
from flask_session import Session
from concurrent.futures import ThreadPoolExecutor
from twilio.rest import Client
from openai import OpenAI
from datetime import datetime
import pytz
import logging
import firebase_admin
from firebase_admin import firestore, credentials
from google.cloud.firestore_v1.base_query import FieldFilter
from google_auth_oauthlib.flow import Flow 
import os

from parsing_utils import intent, parse_timezone, parse_set, parse_delete, parse_edit, parse_calendar
from reminder_utils import get_reminders, handle_reminders, update_recurring_reminders
from calendar_utils import list_calendar

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
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(handle_reminders, event) for event in reminders]
    for f in futures:
        f.result()
    return jsonify({"Status": "Reminders sent"})

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
