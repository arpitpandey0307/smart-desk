import os
import sqlite3
import logging
import google.cloud.logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

from google.adk import Agent
from google.adk.agents import SequentialAgent
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.langchain_tool import LangchainTool

from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper

import google.auth
import google.auth.transport.requests
import google.oauth2.id_token

# --- Setup Logging and Environment ---
cloud_logging_client = google.cloud.logging.Client()
cloud_logging_client.setup_logging()

load_dotenv()
model_name = os.getenv("MODEL")

# ----------------------------
# DATABASE SETUP
# ----------------------------

DB_PATH = "/tmp/smartdesk.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            deadline TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            tags TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT,
            action TEXT,
            timestamp TEXT
        );
    """)
    conn.commit()
    conn.close()

init_db()

def log_agent(agent_name: str, action: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO agent_logs (agent_name, action, timestamp) VALUES (?, ?, ?)",
        (agent_name, action, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ----------------------------
# TOOLS
# ----------------------------

def save_user_prompt(tool_context: ToolContext, prompt: str) -> dict:
    """Saves the user's request to shared state for all agents."""
    tool_context.state["PROMPT"] = prompt
    logging.info(f"[SmartDesk] Prompt saved: {prompt}")
    return {"status": "success"}

def create_task(tool_context: ToolContext, title: str, priority: str = "medium", deadline: str = "") -> dict:
    """Creates a new task and saves it to the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (title, priority, deadline, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (title, priority, deadline, datetime.now().isoformat())
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    log_agent("task_agent", f"Created task: {title}")
    return {"status": "success", "task_id": task_id, "title": title, "priority": priority, "deadline": deadline}

def get_all_tasks(tool_context: ToolContext) -> dict:
    """Fetches all tasks from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, priority, deadline, status FROM tasks")
    rows = cursor.fetchall()
    conn.close()
    tasks = [{"id": r[0], "title": r[1], "priority": r[2], "deadline": r[3], "status": r[4]} for r in rows]
    log_agent("task_agent", f"Fetched {len(tasks)} tasks")
    return {"tasks": tasks}

def schedule_event(tool_context: ToolContext, title: str, start_time: str, end_time: str) -> dict:
    """Schedules a calendar event after checking for conflicts."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT title FROM events WHERE start_time < ? AND end_time > ?",
        (end_time, start_time)
    )
    conflict = cursor.fetchone()
    if conflict:
        conn.close()
        return {"status": "conflict", "message": f"Conflict with existing event: {conflict[0]}"}
    cursor.execute(
        "INSERT INTO events (title, start_time, end_time) VALUES (?, ?, ?)",
        (title, start_time, end_time)
    )
    conn.commit()
    conn.close()
    log_agent("calendar_agent", f"Scheduled: {title}")
    return {"status": "success", "title": title, "start": start_time, "end": end_time}

def block_daily_focus(tool_context: ToolContext, title: str, start_hour: int, duration_hours: int, days: int = 5) -> dict:
    """Blocks focus time for N consecutive days starting from today."""
    results = []
    base = datetime.now()
    for i in range(days):
        day = base + timedelta(days=i)
        start = day.replace(hour=start_hour, minute=0, second=0, microsecond=0).isoformat()
        end = day.replace(hour=start_hour + duration_hours, minute=0, second=0, microsecond=0).isoformat()
        result = schedule_event(tool_context, title, start, end)
        results.append(result)
    log_agent("calendar_agent", f"Blocked daily focus: {title} for {days} days")
    return {"status": "success", "blocked_slots": results}

def save_note(tool_context: ToolContext, content: str, tags: str = "") -> dict:
    """Saves a note or summary to the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO notes (content, tags, created_at) VALUES (?, ?, ?)",
        (content, tags, datetime.now().isoformat())
    )
    note_id = cursor.lastrowid
    conn.commit()
    conn.close()
    log_agent("notes_agent", f"Saved note (id: {note_id})")
    return {"status": "success", "note_id": note_id, "content": content}

# Wikipedia tool for research agent
wikipedia_tool = LangchainTool(
    tool=WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper())
)

# ----------------------------
# AGENTS
# ----------------------------

# 1. Task Agent
task_agent = Agent(
    name="task_agent",
    model=model_name,
    description="Creates and manages tasks in the database.",
    instruction="""
    You are the Task Manager Agent for SmartDesk.
    Based on the user's PROMPT, break it down into clear tasks.
    Create each task using the create_task tool with proper title, priority, and deadline.
    For complex goals, create multiple tasks (one per milestone).

    PROMPT: { PROMPT }
    """,
    tools=[create_task, get_all_tasks],
    output_key="task_output"
)

# 2. Calendar Agent
calendar_agent = Agent(
    name="calendar_agent",
    model=model_name,
    description="Schedules events and blocks focus time on the calendar.",
    instruction="""
    You are the Calendar Agent for SmartDesk.
    Based on the user's PROMPT and tasks already created, schedule calendar blocks.
    If the user wants daily focus time, use block_daily_focus tool.
    If it's a specific meeting or event, use schedule_event tool.
    Always report any conflicts you find.

    PROMPT: { PROMPT }
    Tasks Created: { task_output }
    """,
    tools=[schedule_event, block_daily_focus],
    output_key="calendar_output"
)

# 3. Notes Agent
notes_agent = Agent(
    name="notes_agent",
    model=model_name,
    description="Captures summaries and action items as notes.",
    instruction="""
    You are the Notes Agent for SmartDesk.
    Summarize everything that was planned and save it as a note using save_note tool.
    Include: what the user wanted, tasks created, and calendar blocks scheduled.
    Tag the note with relevant keywords.

    PROMPT: { PROMPT }
    Tasks: { task_output }
    Calendar: { calendar_output }
    """,
    tools=[save_note],
    output_key="notes_output"
)

# 4. Research Agent
research_agent = Agent(
    name="research_agent",
    model=model_name,
    description="Answers general knowledge questions using Wikipedia.",
    instruction="""
    You are the Research Agent for SmartDesk.
    Only activate if the user's PROMPT contains a question needing external knowledge.
    If no research is needed, just say 'No research required.'

    PROMPT: { PROMPT }
    """,
    tools=[wikipedia_tool],
    output_key="research_output"
)

# 5. Response Formatter Agent
response_formatter = Agent(
    name="response_formatter",
    model=model_name,
    description="Combines all agent outputs into one clean friendly response.",
    instruction="""
    You are the final voice of SmartDesk — an AI Chief of Staff.
    Combine all results into one structured, friendly response.

    Use this format:
    ✅ Tasks Created: { task_output }
    📅 Calendar Scheduled: { calendar_output }
    📝 Notes Saved: { notes_output }
    🔍 Research: { research_output }

    Be concise and confirm everything done.
    End with: 'Anything else you'd like me to handle?'
    """
)

# ----------------------------
# WORKFLOW
# ----------------------------

smartdesk_workflow = SequentialAgent(
    name="smartdesk_workflow",
    description="Full SmartDesk pipeline: tasks → calendar → notes → research → response",
    sub_agents=[
        task_agent,
        calendar_agent,
        notes_agent,
        research_agent,
        response_formatter
    ]
)

# ----------------------------
# ROOT AGENT
# ----------------------------

root_agent = Agent(
    name="smartdesk_root",
    model=model_name,
    description="SmartDesk entry point — your AI Chief of Staff.",
    instruction="""
    Welcome the user to SmartDesk — their personal AI Chief of Staff.
    Tell them they can ask you to manage tasks, schedule time, save notes, or research anything.
    When the user gives a request, use 'save_user_prompt' to save it,
    then transfer control to 'smartdesk_workflow'.
    """,
    tools=[save_user_prompt],
    sub_agents=[smartdesk_workflow]
)