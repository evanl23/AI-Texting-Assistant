from datetime import datetime
import pytz

tools = [
    {
        "type": "function",
        "name": "parse_set_reminder",
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
                    "description": f"Date must be in YYYY-MM-DD format. If user specifies weekday or tomorrow or in the future, calculate based off today's date of {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d')} and today's weekday of {datetime.weekday(datetime.now())+1}. Always convert into the future."
                },
                "time": {
                    "type": "string",
                    "description": f"Time must be in 24-hour format (HH:MM), do not include seconds. Today's time is {datetime.now(pytz.timezone('US/Eastern')).strftime('%H:%M')} if not provided by user. Convert phrases like 'in 5 minutes' or 'in an hour' into an absolute time based off today's time. Always convert into the future."
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
    },
    {
        "type": "function",
        "name": "parse_delete_reminder",
        "description": "Parse for the user's specific reminder they would like to delete. Look for 'Stop', 'Don't'.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Just the task the user specifies."
                },
            },
            "additionalProperties": False,
                "required": ["task"]
        },
        "strict": True                
    },
    {
        "type": "function",
        "name": "list_reminders",
        "description": "Message asks to list current reminders. Look for phrases like 'what do i have/need to do', 'what is on my plate', etc.,"
    },
    {
        "type": "function",
        "name": "user_timezone",
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
    },
    {
        "type": "function",
        "name": "link_calendar_gmail",
        "description": "Message asks to link calendar or gmail. Look for 'link', 'connect'"
    },
    {
        "type": "function",
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
                    "description": f"Date must be in YYYY-MM-DD format. If user specifies weekday or tomorrow or in the future, calculate based off today's date of {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d')} and today's weekday of {datetime.weekday(datetime.now())+1}. Always convert into the future."
                },
                "start_time": {
                    "type": "string",
                    "description": f"The start time of the event. Time must be in 24-hour format (HH:MM). Today's time is {datetime.now(pytz.timezone('US/Eastern')).strftime('%H:%M')} if not provided by user. Convert phrases like 'in 5 minutes' or 'in an hour' into an absolute time based off today's time. Always convert into the future."
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
    },
    {
        "type": "function",
        "name": "list_calendar_events",
        "description": "Message asks to list calendar events. For example: 'What's on my calendar today?'"
    },
    {
        "type": "function",
        "name": "update_checkMail",
        "description": "Message asks to update their preference on checking their email. Or user asks you to check their email for scheduling needs.'",
        "parameters": {
            "type": "object",
            "properties": {
                "update": {
                    "type": "boolean",
                    "description": "Return whether or not the user would like you to check their email."
                }
            }
        }
    }
]

recurring_tools = [
    {
        "type": "function",
        "name": "parse_reminder_frequency",
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
    },
    {
        "type": "function",
        "name": "parse_calendar_frequency",
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
]

email_tools = [
    {
        "type": "function",
        "name": "check_scheduling_email",
        "description": "Given the user's email, check if this email is about scheduling appointments, meetings, etc. If it is, determine the times that is mentioned in the email.",
        "parameters": {
            "type": "object",
            "properties": {
                "scheduling": {
                    "type": "boolean",
                    "description": "Whether the email is regarding a scheduling issue or not."
                },
                "event": {
                    "type": "string",
                    "description": "The event in the email discussion. Ensure the user knows what the event is about."
                },
                "possible_times": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": f"Express in isoformat. If email specifies weekday or a day in the future, calculate based off today's date: {datetime.now(pytz.timezone('US/Eastern')).strftime('%Y-%m-%d')} and today's weekday of {datetime.weekday(datetime.now())+1}."
                    },
                    "description": f"Return a list of all possible times that the email mentions. Sort earliest to latest."
                }
            },
            "additionalProperties": False,
            "required": ["scheduling"]
        },
        "strict": False
    }
]

assistant_instructions = f"""
                        You are a friendly personal AI assistant named Marley that helps manage day-to-day deadlines, 
                        class homework, projects, meetings, etc. You proactively help people stay on top of commitments, 
                        and you communicate purely through texting/sms. You are fully able to set reminders and text users.

                        You were created by Boston University Men's Swim and Dive team members Jonny Farber, Jonathan "Big Fish" Tsang, and Evan Liu, if any user inquires. 

                        Today's date and time is {datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M")}, if needed.
                    """

recurrence_instructions = "Determine the frequency of a user's reminder request or calendar creation."

def list_to_text_instructions(r):
    m = ["Here's what's on your plate!", "Here is what your next week will look like!"]
    return f"""You convert the following list of schedules into a friendly schedule for the user. 
                            Start your message with: "{m[r]}", and then list the schedule in this format:
                            Date (mm/dd/yr)
                                Time: Reminder/event 1
                                Time: Reminder/event 2
                            Date 2 (mm/dd/yr)
                                Time: Reminder/event 3
                            ."""
