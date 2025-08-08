from flask import Flask, request, jsonify, redirect, session
from flask_session import Session
from concurrent.futures import ThreadPoolExecutor
from twilio.rest import Client
from openai import OpenAI
from datetime import datetime
import json
import pytz
import logging
import firebase_admin
from firebase_admin import firestore, credentials
from googleapiclient.discovery import build
from google.cloud.firestore_v1.base_query import FieldFilter
from google_auth_oauthlib.flow import Flow 
import os

from utils.tools_instructions import tools, recurring_tools, email_tools
from utils.tools_instructions import assistant_instructions, recurrence_instructions, list_to_text_instructions
from utils.reminder_utils import get_reminders, handle_reminders, update_recurring_reminders, add_reminder, delete_reminder
from utils.calendar_utils import list_calendar, add_to_calendar
from utils.memory import setSummary, setFacts, getSummary, getFacts
from utils.gmail import build_gmail_service, check_new_emails, send_reply
from utils.time_utils import find_conflict

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Set up server side session
app.config["SESSION_TYPE"] = 'filesystem'
app.config["SESSION_PERMANENT"] = False
Session(app)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')
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

# Set up Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/calendar",         # Calendar scope
    "https://www.googleapis.com/auth/gmail.readonly",   # Gmail read only scope
    "https://www.googleapis.com/auth/gmail.compose",    # Gmail compose draft scope
]

"""
    Helper methods: 
"""

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
    # Create a response message to send back to the user
    response = Oclient.responses.create(
        model="gpt-4.1",
        input=[{
                    "role": "user",
                    "content": [
                            {
                            "type": "input_text",
                            "text": f"{schedule}"
                        }
                    ]
                }],
        instructions= list_to_text_instructions(r)
        )
    return response.output_text

def create_response(instructions, role, message, tools):
    try:
        response = Oclient.responses.create(
            instructions=instructions,
            model="gpt-4.1",
            input=[{"role": role, "content": message}],
            tools=tools
        )
        return response
    except Exception as e:
        logger.exception("OpenAI response not created")

def process_user_email(user):
    user_dict = user.to_dict()
    Twilio_id = user_dict.get("twilio_ID")
    credential = user_dict.get("google_token")
    Timezone = user_dict.get("profile").get("timezone", "US/Eastern")
    email_address = user_dict.get("profile").get("email")

    # Get their latest emails
    service = build_gmail_service(credential)
    emails = check_new_emails(service, email_address)

    # Check if any of them are scheduling emails
    for email in emails:
        logger.info("Checking %s of user ID: %s for scheduling emails...", email_address, user.id)

        msg_id, body, sender, subject, message_id, thread_id = email
        response = create_response(
            instructions="You determine if this user's email is asking about scheduling.", 
            role="developer", 
            message=f"Subject: {subject}, Body: {body}", 
            tools=email_tools)
        arguments = response.output[0].arguments # Load tool call arguments
        parsed_data = json.loads(arguments)
        to_schedule = parsed_data.get("scheduling", False)
        
        if to_schedule: # The email is asking to schedule 
            event = parsed_data.get("event")
            possible_times = parsed_data.get("possible_times")
            user_events = list_calendar(credential)

            # Check if there is an available time to schedule
            logger.info("Finding best available times for %s of user ID %s among %d calendar events", email_address, user.id, len(user_events))
            best_time = find_conflict(possible_times, user_events, Timezone)
            logger.info("Best time found: %s", best_time)
            
            if best_time: # Only schedule if a best time was found

                # Separate best time into time and date components
                start_time = best_time.strftime("%H:%M")
                date = best_time.strftime("%Y-%m-%d")

                status = add_to_calendar(credential, event, date, start_time, Timezone) # Add event to calendar
                if status == 1:
                    send_to_user = create_response(
                        f"Inform the user you have added this event to their calendar based on this email from {sender}. Start your response with who the user received the email from and what they were asking. Then you can inform the user what you scheduled.", 
                        "developer",
                        f"Subject: {subject}, Event: {event}, time: {start_time}, date: {date}", 
                        tools=None
                        )
                    message_final = send_to_user.output_text
                    message = Tclient.conversations.v1.conversations( # Send confirmation back to user
                            Twilio_id
                        ).messages.create(
                            body=message_final
                        )
                    reply_response = create_response(f"Pretend you are the user and create an automatic response to this email confirming the time: {start_time} and date: {date}. No need to write a subject, just the email body is fine, including greeting and sign-off.",
                        "developer",
                        f"Original email: {body}",
                        tools=None
                        )
                    reply = reply_response.output_text 
                    send_reply(service, reply, sender, subject, email_address, message_id, thread_id) # Propose reply back to sender

                    # service.users().messages().modify( # Set email as read
                    #     userId='me',
                    #     id=msg_id,
                    #     body={'removeLabelIds': ['UNREAD']}
                    # ).execute()
            else:
                logger.warning("Could not find time to schedule event for %s", email_address)

# Endpoint for creating conversation once phone number is received
@app.route("/create_conversation", methods=["POST"])
def create_conversation():
    user_phone = request.form.get("phone", "+12345678900")
    if len(user_phone) == 10:
        user_phone = "+1" + user_phone

    user_ref = db.collection("Users").document(f"{user_phone}").get()

    logger.info("Received user number: %s from website", user_phone)

    if user_ref.exists:
        user_dict = user_ref.to_dict()
        Twilio_id = user_dict.get("twilio_ID")

        message_final = "Hello! You have already signed up."

        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body=message_final
        )
        
        logger.warning("This phone number already exists': %s", user_phone)
        return jsonify({'This phone number already exists': f"{user_phone}"})

    else:

        # Create new twilio conversation
        conversation = Tclient.conversations.v1.conversations.create(
                friendly_name=f"Conversation with {user_phone}"
            )
        
        # Add conversation to firestore collection where document ID is phone number 
        user_ref = db.collection("Users").document(f"{user_phone}")
        user_ref.set({"twilio_ID": f"{conversation.sid}"}, merge=True)

        try: 
            # Add participant to new conversation
            participant = Tclient.conversations.v1.conversations(
                    conversation.sid
                ).participants.create(
                    messaging_binding_address=user_phone,
                    messaging_binding_proxy_address=TWILIO_PHONE_NUMBER
                )
        except Exception as e:
            logger.exception("Invalid message binding address with this number: %s", user_phone)

        response = create_response(assistant_instructions, "user", "Hello, introduce yourself!", tools=None)
        send = response.output_text

        # Send initial message
        message = Tclient.conversations.v1.conversations(
                conversation.sid
            ).messages.create(
                body=send
            )
        
        # Set up user profile
        user_ref.set({
            "memory": 
            {"facts": [], "summary": [], "summarized": True}, 
            "profile": 
            {"googleConnected": False, "name": None, "timezone": None, "preferences": {"dailyBriefing": False, "nudge": False, "checkMail": False}, "workHours": {"start": None, "end": None}}
            },
            merge=True)
        
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
        
        logger.info("Message created with %s", user_phone)
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
    logger.info("Message recieved from %s", from_number)

    # Get twilio id
    user_ref = db.collection("Users").document(f"{from_number}")
    user = user_ref.get()
    if user.exists:
        user_dict = user.to_dict()
        Twilio_id = user_dict.get("twilio_ID")
        Timezone = user_dict.get("profile").get("timezone", "US/Eastern")

        response = create_response(assistant_instructions, "user", user_message, tools) # Create assistant response to user message

        if hasattr(response.output[0], 'name') and hasattr(response.output[0], 'arguments'): # Check if tool calls were used

            # Load tool call name and argument
            tool_name = response.output[0].name
            arguments = response.output[0].arguments
            parsed_data = json.loads(arguments)

            if tool_name == "parse_set_reminder": # Set reminder
                task = parsed_data.get("task")
                date = parsed_data.get("date") 
                time = parsed_data.get("time")
                recurring = parsed_data.get("recurring")
                if recurring == True: # Check if reminder is recurring
                    recurrence = create_response(recurrence_instructions, "developer", user_message, recurring_tools)
                    parsed_frequency = recurrence.output[0].arguments
                    frequency = json.loads(parsed_frequency)
                else:
                    frequency = None
                if task and time:
                    status = add_reminder(from_number, db, task, date, time, Timezone, recurring, frequency)
                    if status == 1:
                        message = create_response(
                            instructions="You create friendly automatic responses to confirm users' reminder requests.",
                            role = "developer",
                            message=user_message,
                            tools=None
                        )
                        message_final = message.output_text
                    else:
                        message_final = "Reminder not set, please try again."
            
            elif tool_name == "parse_delete_reminder": # Delete reminder
                task = parsed_data.get("task")
                date = parsed_data.get("date", None)
                time = parsed_data.get("time", None)
                delete_reminder(from_number, db, task, date, time)
                message = create_response(
                    instructions="You create friendly automatic reponses to confirm users' reminder deletion.",
                    role = "developer",
                    message=user_message,
                    tools=None
                )
                message_final = message.output_text

            elif tool_name == "list_reminders": # List reminders
                p = get_reminders(from_number, db, Timezone)
                message_final = convert_list_to_text(p,0)
            
            elif tool_name == "user_timezone": # Set timezone
                timezone = parsed_data.get("timezone")
                user_ref.update({"profile.timezone": timezone})
                message_final = "Noted!"

            elif tool_name == "link_calendar_gmail": # Link calendar and gmail
                message = Tclient.conversations.v1.conversations(
                    Twilio_id
                ).messages.create(
                    body=f"https://textmarley-one-21309214523.us-central1.run.app/authorize?phone={from_number}"
                )
                message_final = "Click this link to give me access to your Google Calendar so I can better help you!"
            
            elif tool_name == "parse_calendar_event": # Set calendar event
                if "google_token" in user_dict:
                    creds = user_dict.get("google_token")
                    
                    event = parsed_data.get("event")
                    date = parsed_data.get("date") 
                    start_time = parsed_data.get("start_time")
                    end_time = parsed_data.get("end_time")
                    duration = parsed_data.get("duration")
                    recurring = parsed_data.get("recurring")
                    if recurring == True: # Check if event is recurring
                        recurrence = create_response(recurrence_instructions, "developer", user_message, recurring_tools)
                        parsed_frequency = recurrence.output[0].arguments
                        frequency = json.loads(parsed_frequency)
                        FREQ = parsed_frequency.get("FREQ")
                        INTERVAL = parsed_frequency.get("INTERVAL")
                        BYDAY = parsed_frequency.get("BYDAY")
                        comma = ","
                        joined = comma.join(BYDAY)
                        status = add_to_calendar(creds, event, date, start_time, Timezone, duration, end_time, FREQ, joined, INTERVAL)
                    else:
                        status = add_to_calendar(creds, event, date, start_time, Timezone, duration, end_time )
                    if status == 1:
                        message = create_response(
                            instructions="You create friendly automatic responses to confirm users' calendar event creation request.", 
                            role="developer",
                            message=user_message,
                            tools=None
                        )
                        message_final = message.output_text
                    else: message_final = "Calendar event not added, please try again."
                else: message_final = "You have not yet connected your calendar yet!"

            elif tool_name == "list_calendar_events": # List calendar events
                if "google_token" in user_dict:
                    creds = user_dict.get("google_token")
                    p = list_calendar(creds)
                    message_final = convert_list_to_text(p,1)
                else:
                    message_final = "You have not yet connected your calendar yet!"

            elif tool_name == "update_checkMail":
                update = parsed_data.get("update")
                user_ref.update({"profile.preferences.checkMail": update})
                message_final = "Updated your preferences!"
       
        else:
            message_final = response.output_text

        # Send response back using twilio conversation
        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body=message_final
        )
        user_ref.update({"memory.summarized": False})
        return jsonify({"Return message": message_final})
    else:
        logger.warning("User %s not found in Firestore database", from_number)
        return jsonify({"Return message": f"User {from_number} not found in database"})

@app.route("/summarize", methods=["POST"])
def summarize():
    # Summarize each user conversation every night
    users = db.collection("Users").where(filter=FieldFilter("memory.summarized", "==", False)).stream()
    executor2 = ThreadPoolExecutor(max_workers=10)
    futures = []
    for user in users:
        user_dict = user.to_dict()
        twilio_ID = user_dict["twilio_ID"]
        futures.append(executor2.submit(setSummary, db, user, user.id, twilio_ID))
    for f in futures:
        f.result()    
    return jsonify({"Message": "Recent messages summarized and stored"})

# Endpoint for sending out reminders
@app.route("/reminder_thread", methods=["POST"])
def reminder_thread():
    now = datetime.now(pytz.UTC).replace(second=0, microsecond=0).isoformat()
    reminders = db.collection("Reminders").where(filter=FieldFilter("time", "==", now)).where(filter=FieldFilter("status", "==", "Pending")).stream()
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(handle_reminders, event, db) for event in reminders]
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
    update_recurring_reminders(db)
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
        logger.info("Stored phone number %s in session", phone_number)
        session["phone_number"] = phone_number
    flow = Flow.from_client_secrets_file("/mnt/secrets5/gcal_credentials", scopes=SCOPES) # Start authorization process
    flow.redirect_uri = "https://textmarley-one-21309214523.us-central1.run.app/oauth2callback" # Redirect user to this link
    auth_url, state = flow.authorization_url(include_granted_scopes='true', access_type='offline', prompt='consent')
    session["state"] = state # Store current state in session
    logger.info("Stored state %s in session", state)
    return redirect(auth_url)

# Endpoint for exchange of authorization code for access token and store credentials with user
@app.route("/oauth2callback", methods=["POST", "GET"])
def oauth2callback():
    """ Handles redirect from Google after the user either grants permission or denies permission. 
        Exchanges authorization code for an access token. 
    """
    _state = session.get("state") # Get state from session 
    logger.info("Retrieved state %s from session", _state)

    flow = Flow.from_client_secrets_file("/mnt/secrets5/gcal_credentials", scopes=SCOPES, state=_state) # Ensure flow is the same flow as state
    flow.redirect_uri = "https://textmarley-one-21309214523.us-central1.run.app/oauth2callback"

    https_authorization_url = request.url.replace('http://', 'https://') # Ensure in https format
    flow.fetch_token(authorization_response=https_authorization_url) # Get access token in exchange for authoriztion code

    credentials = flow.credentials

    number = session.get("phone_number")
    logger.info("Retrieved phone number %s from session", number)
    user_ref = db.collection("Users").document(number)

    gmail_service = build("gmail", "v1", credentials=credentials)
    profile = gmail_service.users().getProfile(userId='me').execute()
    email = profile.get("emailAddress")
    
    user_dict = user_ref.get().to_dict()
    if "google_token" in user_dict:
        user_ref.update({"google_token.token": credentials.token,
                         "google_token.refresh_token": credentials.refresh_token,
                         "google_token.scopes": credentials.scopes,
                         "profile.googleConnected": True})
        user_ref.set({"profile": {"email": email}}, merge = True)
        return "<p>You already have a Google Calendar and Gmail connected! You may exit this window."
    else: 
        user_ref.update({
            "profile.googleConnected": True
        })
        
        user_ref.set({
            "profile": {"email": email},
            "google_token": credentials_to_dict(credentials) # Store calendar credentials with user
        }, merge=True)

        Twilio_id = user_dict.get("twilio_ID")
        message = Tclient.conversations.v1.conversations(
            Twilio_id
        ).messages.create(
            body="Calendar and Gmail linked!"
        )
        return "<p>Your Google Calendar and Gmail are now linked! You may exit this window.</p>"

@app.route("/check_emails", methods=["POST"])
def check_mail():
    # Filter for users who have calendar and gmail connected and would like Marley to check their email
    users = db.collection("Users").where(filter=FieldFilter("profile.googleConnected", "==", True)).where(filter=FieldFilter("profile.preferences.checkMail", "==", True)).stream()

    with ThreadPoolExecutor(max_workers=10) as email_executor:
        futures = [email_executor.submit(process_user_email, user) for user in users]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.exception("Error in processing email")
    
    return jsonify({"status": "Checked email for scheduling intents."})

# Endpoint for testing
@app.route("/testing", methods=["GET"])
def testing():

    return f"<p>This is the ship that made the Kessel Run in fourteen parsecs?</p>"

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
