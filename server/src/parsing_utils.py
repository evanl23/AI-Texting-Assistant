from openai import OpenAI
from datetime import datetime
import pytz
import json
import os

from reminder_utils import add_reminder, delete_reminder, get_reminders
from calendar_utils import add_to_calendar

# Set up OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
Oclient = OpenAI(api_key=OPENAI_API_KEY)

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