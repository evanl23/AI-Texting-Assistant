import os

# Set up logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')
logger = logging.getLogger(__name__)

# Import OpenAI
from openai import OpenAI
_oclient = None
def get_openai_client():
    """Initialize and return the OpenAI client"""
    global _oclient
    if _oclient is None:
        # Api key
        openai_api_key = os.getenv("OPENAI_API_KEY")
        _oclient = OpenAI(api_key=openai_api_key)
    return _oclient

# Get Twilio Client
from twilio.rest import Client
_tclient = None
def get_twilio_client():
    """Initialize and return the Twilio client"""
    global _tclient
    if _tclient is None:
        # Api keys
        twilio_sid = os.getenv("TWILIO_SID")
        twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        _tclient = Client(twilio_sid, twilio_auth_token)
    return _tclient


def getSummary(db, user_number) -> str:
    user_ref = db.collection("Users").document(user_number)
    try:
        user_dict = user_ref.get().to_dict().get("memory")
        return user_dict["summary"]
    except Exception as e:
        logger.exception("Failed to fetch summary from Firestore for user %s", user_number)


def getFacts(db, user_number) -> str:
    user_ref = db.collection("Users").document(user_number)
    try:
        user_dict = user_ref.get().to_dict().get("memory")
        return user_dict["facts"]
    except Exception as e:
        logger.exception("Failed to fetch summary from Firestore for user %s", user_number)


def setSummary(db, user_ref, user_number, TwilioID):
    # Summarize the recent messages
    Tclient = get_twilio_client()
    user = Tclient.conversations.v1.conversations(
        TwilioID
    ).messages.list(limit=10)
    messages = []
    for record in user:
        messages.append(record.body)
    logger.info("Summarizing %d messages from user: %s", len(messages), user_number)

    Oclient = get_openai_client()
    summary = Oclient.responses.create(
        model="gpt-4o-mini",
        instructions= "Summarize the user's messages.",
        input= messages
        )
    Summary = summary.output_text
    try:
        user_dict = user_ref.to_dict()
        _summary = user_dict["memory"]["summary"]
        update = _summary.append(Summary)
        db.collection("Users").document(user_number).update({"memory.summary": update})
        db.collection("Users").document(user_number).update({"memory.summarized": True})
    except Exception as e:
        logger.exception("Error updating summary for user %s", user_number)

def setFacts():
    None