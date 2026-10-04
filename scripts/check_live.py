"""Opt-in MCP smoke check. Print statuses and counts, never school-data contents."""

import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "server.py")], cwd=str(root))
    failed = 0
    passed = 0
    skipped = 0
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            registry = await session.list_tools()
            print(f"MCP initialization OK. {len(registry.tools)} tools registered.")

            async def check(name, arguments):
                nonlocal failed, passed
                result = await session.call_tool(name, arguments)
                if result.isError:
                    failed += 1
                    print(f"{name}: FAILED")
                    return None
                data = json.loads(result.content[0].text)
                if "error" in data:
                    failed += 1
                    print(f"{name}: FAILED")
                    return None
                passed += 1
                counts = {key: value for key, value in data.items()
                          if key.endswith("_count") and isinstance(value, int)}
                print(f"{name}: OK {json.dumps(counts)}")
                return data

            today = date.today()
            recent = {"date_from": (today - timedelta(days=30)).isoformat(), "date_to": today.isoformat()}
            for name in ("get_student_info", "get_grades", "get_averages", "get_absences", "get_homework", "get_menus"):
                await check(name, {})
            await check("get_timetable", {"date": today.isoformat(), "date_to": (today + timedelta(days=7)).isoformat()})
            discussions = await check("get_discussions", {})
            if discussions and discussions["discussions"]:
                await check("get_discussion_messages", {"discussion_id": discussions["discussions"][0]["id"]})
            else:
                skipped += 1
                print("get_discussion_messages: SKIPPED. No discussion available.")
            notices = await check("get_information_and_surveys", {})
            notice = next((row for row in notices["information_and_surveys"] if not row["survey"]), None) if notices else None
            if notice:
                await check("get_information", {"information_id": notice["id"]})
            else:
                skipped += 1
                print("get_information: SKIPPED. No announcement available in the first page.")
            for name in ("get_lesson_contents", "get_teaching_resources"):
                await check(name, recent)
            for name in ("get_report_card", "get_evaluations", "get_teaching_staff", "get_recipients", "export_calendar", "export_timetable_pdf"):
                result = await check(name, {})
                if result is not None and result.get("available") is False:
                    print(f"{name}: Not published or not exposed by this account.")
            if discussions and discussions["discussions"]:
                identifier = discussions["discussions"][0]["id"]
                await check("get_discussion_participants", {"discussion_id": identifier})
                messages = await session.call_tool("get_discussion_messages", {"discussion_id": identifier})
                data = json.loads(messages.content[0].text) if not messages.isError else {}
                if data.get("messages"):
                    await check("get_message_recipients", {"discussion_id": identifier, "message_id": data["messages"][0]["id"]})
            if notices:
                survey = next((row for row in notices["information_and_surveys"] if row["survey"]), None)
                if survey:
                    await check("get_survey", {"information_id": survey["id"]})
                else:
                    print("get_survey: SKIPPED. No survey available in the first page.")
    print(f"Live result: {passed} passed, {failed} failed, {skipped} skipped.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
