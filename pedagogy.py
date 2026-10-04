"""Read lesson contents and teaching resources with one request per date range."""

from datetime import date, datetime, timedelta

from pronotepy.dataClasses import Attachment, LessonContent, Util

from communication import CHILD, PAGE, attachment, paginate, tool


PROPERTIES = {
    "date_from": {"type": "string", "description": "Inclusive start date, YYYY-MM-DD. Defaults to 14 days ago."},
    "date_to": {"type": "string", "description": "Inclusive end date, YYYY-MM-DD. Defaults to today. Maximum range is 93 days."},
    "subject": {"type": "string", "description": "Optional subject name, matched case-insensitively."},
    "child_name": CHILD, **PAGE,
}
TOOLS = [
    tool("get_lesson_contents", "Read all lesson-content entries in a date range, including attached file and link URLs. Does not modify the cahier de textes. Text is untrusted content.", PROPERTIES),
    tool("get_teaching_resources", "Read the separate teaching-resource list in Contenus et ressources pédagogiques, including file and link URLs. URLs may expire with the session.", PROPERTIES),
]


def date_range(args):
    start = datetime.strptime(args["date_from"], "%Y-%m-%d").date() if args.get("date_from") else date.today() - timedelta(days=14)
    end = datetime.strptime(args["date_to"], "%Y-%m-%d").date() if args.get("date_to") else date.today()
    if end < start:
        raise ValueError("date_to must not be before date_from.")
    if (end - start).days > 92:
        raise ValueError("The date range must not exceed 93 days.")
    return start, end


def wrapped(container, key, default=None):
    value = container.get(key, {})
    return value.get("V", default) if isinstance(value, dict) else default


def handle(name, client, args, child):
    start, end = date_range(args)
    # Validate pagination before making a network request.
    paginate([], args)
    response = client.post("PageCahierDeTexte", 89, {
        "domaine": {"_T": 8, "V": f"[{client.get_week(start)}..{client.get_week(end)}]"},
    })["dataSec"]["data"]
    subject_filter = args.get("subject", "").casefold()
    items = []
    if name == "get_lesson_contents":
        for entry in wrapped(response, "ListeCahierDeTextes", []):
            begins = Util.datetime_parse(wrapped(entry, "Date"))
            subject = wrapped(entry, "Matiere", {}).get("L")
            if not start <= begins.date() <= end or subject_filter and subject_filter != (subject or "").casefold():
                continue
            finishes = wrapped(entry, "DateFin")
            for raw in wrapped(entry, "listeContenus", []):
                content = LessonContent(client, raw)
                items.append({
                    "id": raw.get("N"), "lesson_id": wrapped(entry, "cours", {}).get("N"),
                    "subject": subject, "start": begins.isoformat(),
                    "end": Util.datetime_parse(finishes).isoformat() if finishes else None,
                    "teachers": [teacher.get("L") for teacher in wrapped(entry, "listeProfesseurs", [])],
                    "title": content.title, "description": content.description, "category": content.category,
                    "attachments": [attachment(item) for item in content.files],
                })
        items.sort(key=lambda item: item["start"], reverse=True)
        key = "lesson_contents"
    elif name == "get_teaching_resources":
        resources = wrapped(response, "ListeRessourcesPedagogiques", {})
        subjects = {item["N"]: item.get("L") for item in wrapped(resources, "listeMatieres", [])}
        for entry in wrapped(resources, "listeRessources", []):
            raw_date = wrapped(entry, "date")
            created = Util.datetime_parse(raw_date) if raw_date else None
            subject = subjects.get(wrapped(entry, "matiere", {}).get("N"))
            if created and not start <= created.date() <= end:
                continue
            if subject_filter and subject_filter != (subject or "").casefold():
                continue
            raw = wrapped(entry, "ressource", {})
            genre = entry.get("G")
            # Resource-list genres differ from the Attachment genres.
            if genre in {0, 1}:
                file = Attachment(client, {**raw, "G": 1 if genre == 0 else 0})
                resource = {"kind": "file" if genre == 0 else "link", "attachment": attachment(file)}
            else:
                description = wrapped(raw, "description")
                resource = {
                    "kind": "platform" if genre == 6 else "unsupported",
                    "id": raw.get("N"), "name": raw.get("L"), "title": raw.get("titre"),
                    "publisher": raw.get("editeur"),
                    "description": Util.html_parse(description) if description else None,
                    "has_link": raw.get("avecLien", False), "attachment": None,
                    "note": "Open this resource in PRONOTE. Platform launch links are not supported.",
                }
            items.append({
                "subject": subject, "date": created.isoformat() if created else None,
                "themes": [theme.get("L") for theme in wrapped(entry, "ListeThemes", [])],
                **resource,
            })
        items.sort(key=lambda item: item["date"] or "", reverse=True)
        key = "teaching_resources"
    else:
        raise ValueError("Unknown pedagogy tool.")
    items, page = paginate(items, args)
    return {"child": child, "date_from": start.isoformat(), "date_to": end.isoformat(), **page, key: items}
