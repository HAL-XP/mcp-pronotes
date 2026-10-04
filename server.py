"""
MCP Server for Pronote (French school platform).

Exposes school data (timetable, grades, homework, absences, student info)
via the Model Context Protocol over stdio transport.

Configuration: environment variables or config.json in the same directory.
  PRONOTE_URL        — Pronote server URL
  PRONOTE_USERNAME   — Parent/student username
  PRONOTE_PASSWORD   — Account password
  PRONOTE_ACCOUNT_TYPE — "parent" (default) or "student"
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pronotepy
from auth import login_from_file, persist_credentials
import communication
import pedagogy
import features
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-pronotes")
logging.getLogger("pronotepy").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, str]:
    """Load config from env vars, falling back to config.json."""
    config: dict[str, str] = {}

    # Try config.json first (as base), env vars override
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
            config["url"] = file_cfg.get("pronote_url", "")
            config["username"] = file_cfg.get("username", "")
            config["password"] = file_cfg.get("password", "")
            config["account_type"] = file_cfg.get("account_type", "parent")
            config["login_file"] = file_cfg.get("login_file", "")

    # Environment variables take precedence
    if os.environ.get("PRONOTE_URL"):
        config["url"] = os.environ["PRONOTE_URL"]
    if os.environ.get("PRONOTE_USERNAME"):
        config["username"] = os.environ["PRONOTE_USERNAME"]
    if os.environ.get("PRONOTE_PASSWORD"):
        config["password"] = os.environ["PRONOTE_PASSWORD"]
    if os.environ.get("PRONOTE_ACCOUNT_TYPE"):
        config["account_type"] = os.environ["PRONOTE_ACCOUNT_TYPE"]
    if os.environ.get("PRONOTE_LOGIN_FILE"):
        config["login_file"] = os.environ["PRONOTE_LOGIN_FILE"]

    return config


CONFIG = _load_config()

# ---------------------------------------------------------------------------
# Pronote client management (cached, with auto-reconnect)
# ---------------------------------------------------------------------------

_client: Optional[pronotepy.ParentClient | pronotepy.Client] = None
_client_timestamp: float = 0.0
_CLIENT_TTL = 25 * 60  # 25 minutes — Pronote sessions expire after ~30 min
_credential_path: Optional[Path] = None
_credential_options: dict[str, str] = {}


def _get_client() -> pronotepy.ParentClient | pronotepy.Client:
    """Return a cached Pronote client, reconnecting if stale or missing."""
    global _client, _client_timestamp, _credential_path, _credential_options

    now = time.time()

    # Reuse existing client if fresh enough
    if _client is not None and (now - _client_timestamp) < _CLIENT_TTL:
        try:
            _client.session_check()
        except Exception:
            logger.warning("Session check failed, reconnecting...")
            _client = None
        else:
            _persist_client_credentials()
            return _client

    # Validate config
    url = CONFIG.get("url", "")
    username = CONFIG.get("username", "")
    password = CONFIG.get("password", "")
    account_type = CONFIG.get("account_type", "parent")

    login_path = Path(CONFIG.get("login_file") or Path(__file__).parent / "login.json").expanduser()
    if login_path.exists():
        _client = None
        _credential_path = None
        _client, account_type, _credential_options = login_from_file(login_path, account_type)
        _credential_path = login_path
        CONFIG["account_type"] = account_type
        _client_timestamp = time.time()
        logger.info("Connected to Pronote using a local mobile token.")
        return _client
    if CONFIG.get("login_file"):
        raise ValueError("The configured login file does not exist.")
    _credential_path = None
    _credential_options = {}

    if not url:
        raise ValueError(
            "PRONOTE_URL is not configured. Set it in config.json or as an environment variable."
        )
    if not username or not password:
        raise ValueError(
            "PRONOTE_USERNAME and PRONOTE_PASSWORD must be configured. "
            "Set them in config.json or as environment variables."
        )

    logger.info("Connecting to Pronote with a %s account...", account_type)

    if account_type == "parent":
        _client = pronotepy.ParentClient(url, username=username, password=password)
    else:
        _client = pronotepy.Client(url, username=username, password=password)

    _client_timestamp = time.time()
    logger.info("Connected to Pronote successfully.")
    return _client


def _persist_client_credentials() -> None:
    if _client is not None and _credential_path is not None:
        persist_credentials(_client, _credential_path, CONFIG.get("account_type", "student"), _credential_options)


def _is_parent() -> bool:
    return CONFIG.get("account_type", "parent") == "parent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: Optional[str]) -> date:
    """Parse YYYY-MM-DD string or return today."""
    if not date_str:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _select_child(client: pronotepy.ParentClient, child_name: Optional[str] = None) -> str:
    """Select a child by name. Returns the selected child's name."""
    if child_name:
        client.set_child(child_name)
        return child_name
    # Reset a cached parent session to the documented default child.
    name = client.children[0].name
    client.set_child(name)
    return name


def _list_children_names(client: pronotepy.ParentClient) -> list[str]:
    return [c.name for c in client.children]


def _format_lesson(lesson: Any) -> dict[str, Any]:
    """Serialize a Lesson object."""
    result: dict[str, Any] = {
        "subject": lesson.subject.name if lesson.subject else None,
        "start": lesson.start.isoformat() if lesson.start else None,
        "end": lesson.end.isoformat() if lesson.end else None,
        "canceled": lesson.canceled,
        "status": lesson.status,
        "room": lesson.classroom,
        "rooms": lesson.classrooms if hasattr(lesson, "classrooms") else None,
        "teacher": lesson.teacher_name,
        "group": lesson.group_name if hasattr(lesson, "group_name") else None,
        "memo": lesson.memo,
        "exempted": lesson.exempted if hasattr(lesson, "exempted") else False,
        "detention": lesson.detention if hasattr(lesson, "detention") else False,
        "test": lesson.test if hasattr(lesson, "test") else False,
    }
    return result


def _format_homework(hw: Any) -> dict[str, Any]:
    return {
        "id": getattr(hw, "id", None),
        "subject": hw.subject.name if hw.subject else None,
        "description": hw.description,
        "due_date": hw.date.isoformat() if hw.date else None,
        "done": hw.done,
        "background_color": hw.background_color,
        "attachments": [communication.attachment(item) for item in hw.files],
    }


def _format_grade(grade: Any) -> dict[str, Any]:
    return {
        "subject": grade.subject.name if grade.subject else None,
        "grade": grade.grade,
        "out_of": grade.out_of,
        "date": grade.date.isoformat() if grade.date else None,
        "average": grade.average if hasattr(grade, "average") else None,
        "max": grade.max if hasattr(grade, "max") else None,
        "min": grade.min if hasattr(grade, "min") else None,
        "coefficient": grade.coefficient if hasattr(grade, "coefficient") else None,
        "comment": grade.comment if hasattr(grade, "comment") else None,
        "is_bonus": grade.is_bonus if hasattr(grade, "is_bonus") else False,
        "is_optional": grade.is_optionnal if hasattr(grade, "is_optionnal") else False,
        "is_out_of_20": grade.is_out_of_20 if hasattr(grade, "is_out_of_20") else False,
        "period": grade.period.name if hasattr(grade, "period") and grade.period else None,
    }


def _format_absence(absence: Any) -> dict[str, Any]:
    return {
        "from": absence.from_date.isoformat() if absence.from_date else None,
        "to": absence.to_date.isoformat() if absence.to_date else None,
        "justified": absence.justified,
        "hours": absence.hours,
        "days": absence.days,
        "reasons": absence.reasons,
    }


def _format_delay(delay: Any) -> dict[str, Any]:
    return {
        "date": delay.date.isoformat() if delay.date else None,
        "minutes": delay.minutes,
        "justified": delay.justified,
        "justification": delay.justification if hasattr(delay, "justification") else None,
        "reasons": delay.reasons,
    }


def _format_average(avg: Any) -> dict[str, Any]:
    return {
        "subject": avg.subject.name if avg.subject else None,
        "student_average": avg.student,
        "class_average": avg.class_average,
        "max": avg.max,
        "min": avg.min,
        "out_of": avg.out_of,
    }


def _error_result(message: str) -> list[types.TextContent]:
    """Return an error as MCP tool content."""
    return [types.TextContent(type="text", text=json.dumps({"error": message}, ensure_ascii=False))]


def _json_result(data: Any) -> list[types.TextContent]:
    """Return JSON data as MCP tool content."""
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("pronotes")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_timetable",
            description=(
                "Get the school timetable (schedule/emploi du temps) for a student. "
                "Returns lessons for a given date or date range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format. Defaults to today.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format. If omitted, returns only the single day.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts with multiple children). Omit to use the first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_grades",
            description=(
                "Get grades (notes) for a student. Returns grades for the current period, "
                "or a specified period, along with class averages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "period_name": {
                        "type": "string",
                        "description": "Name of the period (e.g. 'Trimestre 1'). Defaults to current period.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit for first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_homework",
            description=(
                "Get upcoming homework (devoirs) for a student. "
                "Returns homework starting from a given date."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format. Defaults to today.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD. Defaults to 14 days from date_from.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit for first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_absences",
            description=(
                "Get absences, delays (retards), and punishments for a student in the current period."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "period_name": {
                        "type": "string",
                        "description": "Name of the period. Defaults to current period.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit for first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_student_info",
            description=(
                "Get general info about connected students: names, classes, school, "
                "available periods, and current period."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit to get info for all children.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_averages",
            description=(
                "Get subject averages (moyennes) for a student in a given period. "
                "Includes student average, class average, min, and max per subject."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "period_name": {
                        "type": "string",
                        "description": "Name of the period. Defaults to current period.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit for first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_menus",
            description=(
                "Get school canteen menus for a date range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format. Defaults to today.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD. Defaults to end of the week.",
                    },
                    "child_name": {
                        "type": "string",
                        "description": "Name of the child (for parent accounts). Omit for first child.",
                    },
                },
                "additionalProperties": False,
            },
        ),
    ] + communication.TOOLS + pedagogy.TOOLS + features.TOOLS


TOOL_HANDLERS = {
    "get_timetable",
    "get_grades",
    "get_homework",
    "get_absences",
    "get_student_info",
    "get_averages",
    "get_menus",
} | {tool.name for tool in communication.TOOLS + pedagogy.TOOLS + features.TOOLS}


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name not in TOOL_HANDLERS:
        return _error_result(f"Unknown tool: {name}")

    try:
        features.authorize(name, arguments)
        client = _get_client()

        # Select child if parent account
        if _is_parent() and isinstance(client, pronotepy.ParentClient):
            child_name = arguments.get("child_name")
            selected = _select_child(client, child_name)
        else:
            selected = None

        if name in {tool.name for tool in communication.TOOLS}:
            return _json_result(communication.handle(name, client, arguments, selected))
        if name in {tool.name for tool in pedagogy.TOOLS}:
            return _json_result(pedagogy.handle(name, client, arguments, selected))
        if name in {tool.name for tool in features.TOOLS}:
            return _json_result(features.handle(name, client, arguments, selected, _find_period))

        if name == "get_timetable":
            return await _handle_timetable(client, arguments, selected)
        elif name == "get_grades":
            return await _handle_grades(client, arguments, selected)
        elif name == "get_homework":
            return await _handle_homework(client, arguments, selected)
        elif name == "get_absences":
            return await _handle_absences(client, arguments, selected)
        elif name == "get_student_info":
            return await _handle_student_info(client, arguments)
        elif name == "get_averages":
            return await _handle_averages(client, arguments, selected)
        elif name == "get_menus":
            return await _handle_menus(client, arguments, selected)
        else:
            return _error_result(f"Unknown tool: {name}")  # unreachable

    except pronotepy.ENTLoginError as e:
        logger.error("ENT authentication failed.")
        return _error_result("ENT authentication failed.")
    except pronotepy.CryptoError as e:
        logger.error("Pronote crypto/authentication failed.")
        global _client
        _client = None  # Force reconnect on next call
        return _error_result("Authentication error. Check the QR PIN or generate a fresh QR code.")
    except pronotepy.ChildNotFound as e:
        logger.error("Child not found: %s", e)
        return _error_result(f"Child not found: {e}")
    except pronotepy.PronoteAPIError as e:
        logger.error("Pronote API error of type %s.", type(e).__name__)
        message = "Pronote API error. This section may be unavailable or the session may have expired."
        if name in features.WRITES:
            message += " The action may have succeeded. Verify in PRONOTE before retrying."
        return _error_result(message)
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        return _error_result(str(e))
    except Exception as e:
        logger.error("Unexpected error in %s of type %s.", name, type(e).__name__)
        message = f"Unexpected error of type {type(e).__name__}."
        if name in features.WRITES:
            message += " The action may have succeeded. Verify in PRONOTE before retrying."
        return _error_result(message)
    finally:
        # PRONOTE can rotate tokens during automatic session refreshes.
        try:
            _persist_client_credentials()
        except Exception:
            logger.error("Could not store the rotated PRONOTE token.")
            raise RuntimeError("Could not store the rotated PRONOTE token. Check login-file permissions.") from None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _find_period(client: pronotepy.Client, period_name: Optional[str] = None):
    """Find a period by name or return current period."""
    if period_name:
        for p in client.periods:
            if p.name.lower() == period_name.lower():
                return p
        # Fuzzy match
        for p in client.periods:
            if period_name.lower() in p.name.lower():
                return p
        available = [p.name for p in client.periods]
        raise ValueError(
            f"Period '{period_name}' not found. Available periods: {', '.join(available)}"
        )
    return client.current_period


async def _handle_timetable(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    d = _parse_date(args.get("date"))
    d_to = _parse_date(args.get("date_to")) if args.get("date_to") else None

    lessons = client.lessons(d, d_to)

    # Sort by start time
    lessons.sort(key=lambda l: l.start if l.start else datetime.min)

    result = {
        "date": d.isoformat(),
        "date_to": d_to.isoformat() if d_to else d.isoformat(),
        "child": child,
        "lesson_count": len(lessons),
        "lessons": [_format_lesson(l) for l in lessons],
    }
    return _json_result(result)


async def _handle_grades(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    period = _find_period(client, args.get("period_name"))

    grades = period.grades
    # Sort by date descending (most recent first)
    grades.sort(key=lambda g: g.date if g.date else date.min, reverse=True)

    overall_avg = None
    class_overall_avg = None
    try:
        overall_avg = period.overall_average
    except Exception:
        pass
    try:
        class_overall_avg = period.class_overall_average
    except Exception:
        pass

    result = {
        "period": period.name,
        "child": child,
        "overall_average": overall_avg,
        "class_overall_average": class_overall_avg,
        "grade_count": len(grades),
        "grades": [_format_grade(g) for g in grades],
    }
    return _json_result(result)


async def _handle_homework(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    d_from = _parse_date(args.get("date_from"))
    d_to = _parse_date(args.get("date_to")) if args.get("date_to") else d_from + timedelta(days=14)

    homework = client.homework(d_from, d_to)
    # Sort by due date
    homework.sort(key=lambda h: h.date if h.date else date.max)

    result = {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "child": child,
        "homework_count": len(homework),
        "homework": [_format_homework(h) for h in homework],
    }
    return _json_result(result)


async def _handle_absences(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    period = _find_period(client, args.get("period_name"))

    absences = period.absences
    delays = period.delays
    punishments = []
    try:
        punishments_raw = period.punishments
        punishments = [
            {
                "date": p.given.isoformat() if hasattr(p, "given") and p.given else None,
                "exclusion": p.exclusion if hasattr(p, "exclusion") else None,
                "during_lesson": p.during_lesson if hasattr(p, "during_lesson") else None,
                "homework": p.homework if hasattr(p, "homework") else None,
                "reasons": [r for r in (p.reasons if hasattr(p, "reasons") else [])],
                "circumstances": p.circumstances if hasattr(p, "circumstances") else None,
                "nature": p.nature if hasattr(p, "nature") else None,
            }
            for p in punishments_raw
        ]
    except Exception as e:
        logger.warning("Could not fetch punishments: %s", e)

    result = {
        "period": period.name,
        "child": child,
        "absence_count": len(absences),
        "absences": [_format_absence(a) for a in absences],
        "delay_count": len(delays),
        "delays": [_format_delay(d) for d in delays],
        "punishment_count": len(punishments),
        "punishments": punishments,
    }
    return _json_result(result)


async def _handle_student_info(
    client: pronotepy.Client, args: dict[str, Any]
) -> list[types.TextContent]:
    result: dict[str, Any] = {
        "account_type": CONFIG.get("account_type", "parent"),
        "pronote_url": CONFIG.get("url", ""),
    }

    if _is_parent() and isinstance(client, pronotepy.ParentClient):
        child_filter = args.get("child_name")
        children_info = []
        for child in client.children:
            if child_filter and child.name.lower() != child_filter.lower():
                continue
            info = {
                "name": child.name,
                "class": child.class_name,
                "school": child.establishment,
                "delegue": child.delegue,
            }
            children_info.append(info)

        result["children"] = children_info
    else:
        result["logged_in"] = True
        result["name"] = client.info.name
        result["class"] = client.info.class_name
        result["school"] = client.info.establishment

    # Periods
    periods_info = []
    current = client.current_period
    for p in client.periods:
        periods_info.append({
            "name": p.name,
            "start": p.start.isoformat() if p.start else None,
            "end": p.end.isoformat() if p.end else None,
            "is_current": p.id == current.id,
        })
    result["periods"] = periods_info
    result["current_period"] = current.name

    return _json_result(result)


async def _handle_averages(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    period = _find_period(client, args.get("period_name"))

    averages = period.averages
    overall_avg = None
    class_overall_avg = None
    try:
        overall_avg = period.overall_average
    except Exception:
        pass
    try:
        class_overall_avg = period.class_overall_average
    except Exception:
        pass

    result = {
        "period": period.name,
        "child": child,
        "overall_average": overall_avg,
        "class_overall_average": class_overall_avg,
        "subject_count": len(averages),
        "averages": [_format_average(a) for a in averages],
    }
    return _json_result(result)


async def _handle_menus(
    client: pronotepy.Client, args: dict[str, Any], child: Optional[str]
) -> list[types.TextContent]:
    d_from = _parse_date(args.get("date_from"))
    if args.get("date_to"):
        d_to = _parse_date(args.get("date_to"))
    else:
        # Default to end of the week (Friday)
        days_until_friday = (4 - d_from.weekday()) % 7
        if days_until_friday == 0 and d_from.weekday() > 4:
            days_until_friday = 7
        d_to = d_from + timedelta(days=max(days_until_friday, 1))

    menus = client.menus(d_from, d_to)

    def _format_food_list(foods: Optional[list]) -> Optional[list[dict]]:
        if not foods:
            return None
        return [
            {
                "name": f.name,
                "labels": [
                    {"name": lb.name, "color": lb.color}
                    for lb in (f.labels if f.labels else [])
                ],
            }
            for f in foods
        ]

    menus_data = []
    for menu in menus:
        menu_entry: dict[str, Any] = {
            "date": menu.date.isoformat() if menu.date else None,
            "name": menu.name,
            "is_lunch": menu.is_lunch,
            "is_dinner": menu.is_dinner,
            "first_meal": _format_food_list(menu.first_meal),
            "main_meal": _format_food_list(menu.main_meal),
            "side_meal": _format_food_list(menu.side_meal),
            "other_meal": _format_food_list(menu.other_meal),
            "cheese": _format_food_list(menu.cheese),
            "dessert": _format_food_list(menu.dessert),
        }
        menus_data.append(menu_entry)

    result = {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "child": child,
        "menu_count": len(menus_data),
        "menus": menus_data,
    }
    return _json_result(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    logger.info("Starting Pronotes MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
