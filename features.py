"""Additional PRONOTE tools and explicit opt-in account mutations."""

from datetime import date, datetime, timedelta
from contextlib import contextmanager
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse
import uuid

from mcp import types
import pronotepy
from pronotepy.dataClasses import Util

from communication import CHILD, PAGE, attachment, paginate, tool
import pedagogy

DOWNLOAD_DIRECTORY = Path(__file__).parent / "downloads"


ID = {"type": "string", "minLength": 1}
PERIOD = {"period_name": {"type": "string"}, "child_name": CHILD}
DISCUSSION = {"discussion_id": ID, "child_name": CHILD}
MESSAGE = {**DISCUSSION, "message_id": ID}
CONFIRM = {"confirm": {"type": "boolean", "const": True, "description": "Explicit user approval of this exact account-changing action."}}
WRITES = {"set_homework_done", "create_discussion", "reply_to_message", "mark_discussion_read", "mark_information_read"}
TOOLS = [
    tool("get_report_card", "Read a published bulletin, subject averages and teacher comments. Unpublished reports return available=false.", PERIOD),
    tool("get_evaluations", "Read competency evaluations and acquisition levels for a period.", {**PERIOD, **PAGE}),
    tool("get_teaching_staff", "Read the équipe pédagogique. Returns available=false if the school does not expose it.", {"child_name": CHILD}),
    tool("get_recipients", "List permitted teacher and staff recipients for communication.", {"child_name": CHILD, **PAGE}),
    tool("get_discussion_participants", "Read participants in one discussion.", DISCUSSION, ("discussion_id",)),
    tool("get_message_recipients", "Read recipients of a message in a specified discussion.", MESSAGE, ("discussion_id", "message_id")),
    tool("export_calendar", "Return a private iCalendar subscription URL. Treat the URL as a credential.", {"child_name": CHILD}),
    tool("export_timetable_pdf", "Generate a weekly timetable PDF and return its private URL.", {
        "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
        "portrait": {"type": "boolean", "default": False}, "child_name": CHILD,
    }),
    tool("download_attachment", "Download an account-owned file into the private local downloads directory. No arbitrary URLs or output paths are accepted. Maximum size is 20 MiB.", {
        "source": {"type": "string", "enum": ["homework", "information", "lesson", "teaching_resource"]},
        "source_id": ID, "attachment_id": ID, "child_name": CHILD,
        "date_from": {"type": "string"}, "date_to": {"type": "string"},
    }, ("source", "source_id", "attachment_id")),
    tool("get_survey", "Read every survey question and attachment without answering or acknowledging. Does not interpret undocumented response fields.", {"information_id": ID, "child_name": CHILD}, ("information_id",)),
]

for name, description, properties, required in [
    ("set_homework_done", "Set homework completion after explicit approval.", {"homework_id": ID, "done": {"type": "boolean"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}}, ("homework_id", "done")),
    ("create_discussion", "Send a new discussion to explicitly selected recipients after approval. Never retry automatically.", {"subject": ID, "message": ID, "recipient_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": 50, "uniqueItems": True}}, ("subject", "message", "recipient_ids")),
    ("reply_to_message", "Send a reply to an explicitly selected message after approval. Never retry automatically.", {**MESSAGE, "message": ID}, ("discussion_id", "message_id", "message")),
    ("mark_discussion_read", "Change a discussion read flag after explicit approval.", {**DISCUSSION, "read": {"type": "boolean"}}, ("discussion_id", "read")),
    ("mark_information_read", "Acknowledge or unacknowledge an announcement after explicit approval. Upstream compatibility may vary.", {"information_id": ID, "read": {"type": "boolean"}}, ("information_id", "read")),
]:
    spec = tool(name, description + " Requires PRONOTE_ENABLE_WRITES=1 and confirm=true.", {**properties, "child_name": CHILD, **CONFIRM}, (*required, "confirm"))
    spec.annotations = types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=name.startswith(("set_", "mark_")))
    TOOLS.append(spec)
next(item for item in TOOLS if item.name == "download_attachment").annotations.readOnlyHint = False


def authorize(name, args):
    if name in WRITES:
        if os.environ.get("PRONOTE_ENABLE_WRITES") != "1":
            raise ValueError("Account-changing tools are disabled. Set PRONOTE_ENABLE_WRITES=1 to enable them.")
        if args.get("confirm") is not True:
            raise ValueError("This action requires explicit user approval and confirm=true.")


def find(rows, identifier, label):
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"{label} ID is required.")
    row = next((row for row in rows if row.id == identifier), None)
    if row is None:
        raise ValueError(f"{label} not found in this account.")
    return row


def discussion(client, args):
    return find(client.discussions(), args.get("discussion_id"), "Discussion")


def homework(client, args):
    options = {"date_from": args.get("date_from", date.today().isoformat()),
               "date_to": args.get("date_to", (date.today() + timedelta(days=14)).isoformat())}
    return client.homework(*pedagogy.date_range(options))


def handle(name, client, args, child, find_period):
    authorize(name, args)
    if name in WRITES:
        with no_write_retry(client):
            return _handle(name, client, args, child, find_period)
    return _handle(name, client, args, child, find_period)


@contextmanager
def no_write_retry(client):
    """Bypass pronotepy's automatic POST retry for account-changing requests."""
    original = client.post

    def post(function_name, onglet=None, data=None):
        if function_name not in {"SaisieMessage", "SaisieTAFFaitEleve", "SaisieActualites"}:
            return original(function_name, onglet, data)
        payload = {}
        if onglet:
            payload["Signature"] = {"onglet": onglet}
            if isinstance(client, pronotepy.ParentClient):
                payload["Signature"]["membre"] = {"N": client._selected_child.id, "G": 4}
        if data:
            payload["data"] = data
        return client.communication.post(function_name, payload)

    client.post = post
    try:
        yield
    finally:
        client.post = original


def _handle(name, client, args, child, find_period):
    if name == "get_report_card":
        period = find_period(client, args.get("period_name"))
        report = period.report
        return {"child": child, "period": period.name, "available": report is not None,
                "report": None if report is None else {
                    "comments": report.comments,
                    "subjects": [{key: getattr(row, key) for key in (
                        "id", "name", "comments", "student_average", "class_average", "min_average",
                        "max_average", "coefficient", "teachers")} for row in report.subjects],
                }}
    if name == "get_evaluations":
        period = find_period(client, args.get("period_name"))
        rows, page = paginate(period.evaluations, args)
        return {"child": child, "period": period.name, **page, "evaluations": [
            {"id": row.id, "name": row.name, "domain": row.domain, "teacher": row.teacher,
             "coefficient": row.coefficient, "description": row.description, "subject": row.subject.name,
             "date": row.date.isoformat(), "paliers": row.paliers,
             "acquisitions": [item.to_dict() for item in row.acquisitions]} for row in rows]}
    if name == "get_teaching_staff":
        # Avoid the upstream helper's KeyError on schools that return an empty page.
        data = client.post("PageEquipePedagogique", 37)["dataSec"]["data"]
        from pronotepy.dataClasses import TeachingStaff
        rows = [TeachingStaff(row) for row in pedagogy.wrapped(data, "liste", [])]
        return {"child": child, "available": "liste" in data, "teaching_staff": [
            {"id": row.id, "name": row.name, "type": row.type, "subjects": [
                {"id": item.id, "name": item.name,
                 "weekly_minutes": item.duration.total_seconds() / 60 if item.duration is not None else None,
                 "parent_subject_name": item.parent_subject_name} for item in row.subjects]} for row in rows]}
    if name == "get_recipients":
        rows, page = paginate(client.get_recipients(), args)
        return {"child": child, **page, "recipients": [{"id": row.id, "name": row.name, "type": row.type} for row in rows]}
    if name == "get_discussion_participants":
        return {"child": child, "participants": discussion(client, args).participants()}
    if name == "get_message_recipients":
        message = find(discussion(client, args).messages, args.get("message_id"), "Message")
        return {"child": child, "recipients": message.recipients()}
    if name == "export_calendar":
        try:
            url = client.export_ical()
        except pronotepy.ParsingError as error:
            if "iCal" not in error.path:
                raise
            return {"child": child, "available": False, "url": None,
                    "reason": "The school does not expose a calendar feed for this account."}
        return {"child": child, "available": True, "url": url, "private": True}
    if name == "export_timetable_pdf":
        day = datetime.strptime(args["date"], "%Y-%m-%d").date() if args.get("date") else date.today()
        return {"child": child, "url": client.generate_timetable_pdf(day=day, portrait=args.get("portrait", False)), "private": True}
    if name == "get_survey":
        row = find(client.information_and_surveys(), args.get("information_id"), "Information")
        if not row.survey:
            raise ValueError("This information is not a survey. Use get_information.")
        row._fetch_content()
        return {"child": child, "id": row.id, "title": row.title,
                "questions": [{"id": question.get("N"),
                               "text": Util.html_parse(pedagogy.wrapped(question, "texte", "")),
                               "attachments": [attachment(item) for item in _question_attachments(client, question)],
                               "response_details_supported": False} for question in row._raw_content or []]}
    if name == "download_attachment":
        return download(client, args)
    if name == "set_homework_done":
        if type(args.get("done")) is not bool:
            raise ValueError("done must be a boolean.")
        row = find(homework(client, args), args.get("homework_id"), "Homework")
        row.set_done(args["done"])
    elif name == "create_discussion":
        ids = args.get("recipient_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("recipient_ids must contain 1 to 50 unique recipient IDs.")
        recipients = client.get_recipients()
        selected = [find(recipients, identifier, "Recipient") for identifier in ids]
        client.new_discussion(subjet=_text(args, "subject"), message=_text(args, "message"), recipients=selected)
    elif name == "reply_to_message":
        row = discussion(client, args)
        if row.closed:
            raise ValueError("This discussion is closed.")
        message = find(row.messages, args.get("message_id"), "Message")
        message.reply(_text(args, "message"))
    elif name in {"mark_discussion_read", "mark_information_read"}:
        if type(args.get("read")) is not bool:
            raise ValueError("read must be a boolean.")
        if name == "mark_discussion_read":
            discussion(client, args).mark_as(args["read"])
        else:
            row = find(client.information_and_surveys(), args.get("information_id"), "Information")
            if row.survey:
                raise ValueError("Survey acknowledgment is not supported.")
            row.mark_as_read(args["read"])
    else:
        raise ValueError("Unknown additional tool.")
    return {"child": child, "action": name, "success": True,
            "note": "If the connection fails, verify the result in PRONOTE before retrying."}


def _text(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise ValueError(f"{key} must contain 1 to 10000 characters.")
    return value


def _question_attachments(client, question):
    from pronotepy.dataClasses import Attachment
    return [Attachment(client, item) for item in pedagogy.wrapped(question, "listePiecesJointes", [])]


def resolve_attachment(client, args):
    source = args.get("source")
    if source == "homework":
        row = find(homework(client, args), args.get("source_id"), "Homework")
        files = [attachment(item) for item in row.files]
    elif source == "information":
        row = find(client.information_and_surveys(), args.get("source_id"), "Information")
        files = [attachment(item) for item in row.attachments()]
    elif source in {"lesson", "teaching_resource"}:
        name = "get_lesson_contents" if source == "lesson" else "get_teaching_resources"
        options = {key: args[key] for key in ("date_from", "date_to") if key in args}
        options["limit"] = 200
        files = []
        while True:
            page = pedagogy.handle(name, client, options, None)
            for row in page["lesson_contents" if source == "lesson" else "teaching_resources"]:
                if source == "lesson" and row["id"] == args.get("source_id"):
                    files.extend(row["attachments"])
                elif source == "teaching_resource" and row.get("attachment") and row["attachment"]["id"] == args.get("source_id"):
                    files.append(row["attachment"])
            if page["next_offset"] is None:
                break
            options["offset"] = page["next_offset"]
    else:
        raise ValueError("Unknown attachment source.")
    file = next((item for item in files if item["id"] == args.get("attachment_id")), None)
    if file is None or file["type"] != 1:
        raise ValueError("File attachment not found in this account. External links cannot be downloaded.")
    return file


def download(client, args):
    file = resolve_attachment(client, args)
    parsed = urlparse(file["url"])
    root = urlparse(client.communication.root_site)
    if parsed.scheme != "https" or parsed.netloc != root.netloc or "/FichiersExternes/" not in parsed.path:
        raise ValueError("The file URL is not a trusted PRONOTE attachment URL.")
    directory = DOWNLOAD_DIRECTORY
    if directory.is_symlink():
        raise ValueError("The downloads directory must not be a symbolic link.")
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    suffix = Path(file["name"]).suffix
    suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) else ".bin"
    output = directory / (uuid.uuid4().hex + suffix)
    size = 0
    temporary = None
    try:
        with client.communication.session.get(file["url"], stream=True, timeout=30, allow_redirects=False) as response:
            if response.status_code != 200 or "text/html" in response.headers.get("Content-Type", "").lower():
                raise ValueError("PRONOTE did not return a file. Refresh the session and try again.")
            if int(response.headers.get("Content-Length", "0")) > 20 * 1024 * 1024:
                raise ValueError("The attachment exceeds the 20 MiB download limit.")
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".download-", delete=False) as handle:
                temporary = Path(handle.name)
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise ValueError("The attachment exceeds the 20 MiB download limit.")
                    handle.write(chunk)
            if not size:
                raise ValueError("PRONOTE returned an empty file.")
            os.replace(temporary, output)
        return {"path": str(output), "original_name": file["name"], "bytes": size}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
