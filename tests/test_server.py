"""Offline tests. All PRONOTE clients are mocks and use fictional data."""

import json
import os
import unittest
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import server


def decode(result):
    return json.loads(result[0].text)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        # Never discover the user's real login file during an offline test.
        exists = patch.object(server.Path, "exists", return_value=False)
        exists.start()
        self.addCleanup(exists.stop)
        credentials = patch.object(server, "_credential_path", None)
        credentials.start()
        self.addCleanup(credentials.stop)

    def test_environment_overrides_file(self):
        config = {
            "pronote_url": "https://example.invalid/pronote/parent.html",
            "username": "parent",
            "password": "fictional",
            "account_type": "parent",
        }
        env = {
            "PRONOTE_URL": "https://example.invalid/pronote/eleve.html",
            "PRONOTE_USERNAME": "student",
            "PRONOTE_PASSWORD": "also-fictional",
            "PRONOTE_ACCOUNT_TYPE": "student",
        }
        with patch.dict(os.environ, env, clear=True), \
                patch.object(server.Path, "exists", return_value=True), \
                patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(config))):
            loaded = server._load_config()
        self.assertEqual(loaded["url"], env["PRONOTE_URL"])
        self.assertEqual(loaded["username"], "student")
        self.assertEqual(loaded["password"], "also-fictional")
        self.assertEqual(loaded["account_type"], "student")

    def test_student_client_selection(self):
        config = {
            "url": "https://example.invalid/pronote/eleve.html",
            "username": "student",
            "password": "fictional",
            "account_type": "student",
        }
        with patch.object(server, "CONFIG", config), \
                patch.object(server, "_client", None), \
                patch.object(server, "_client_timestamp", 0), \
                patch.object(server.pronotepy, "Client") as student, \
                patch.object(server.pronotepy, "ParentClient") as parent:
            self.assertIs(server._get_client(), student.return_value)
            student.assert_called_once_with(config["url"], username="student", password="fictional")
            parent.assert_not_called()

    def test_cached_client_checks_session(self):
        client = Mock()
        with patch.object(server, "_client", client), \
                patch.object(server, "_client_timestamp", server.time.time()), \
                patch.object(server.pronotepy, "Client") as constructor:
            self.assertIs(server._get_client(), client)
            client.session_check.assert_called_once()
            constructor.assert_not_called()

    def test_missing_configuration_never_connects(self):
        for config in ({}, {"url": "https://example.invalid/pronote/eleve.html"}):
            with self.subTest(config=config), \
                    patch.object(server, "CONFIG", config), \
                    patch.object(server, "_client", None), \
                    patch.object(server.pronotepy, "Client") as constructor:
                with self.assertRaises(ValueError):
                    server._get_client()
                constructor.assert_not_called()


class HelperTests(unittest.TestCase):
    def test_default_child_resets_cached_selection(self):
        client = Mock(children=[NS(name="Fictional Child")])
        self.assertEqual(server._select_child(client), "Fictional Child")
        client.set_child.assert_called_once_with("Fictional Child")

    def test_parse_date(self):
        self.assertEqual(server._parse_date("2026-10-05"), date(2026, 10, 5))
        self.assertEqual(server._parse_date(None), date.today())
        with self.assertRaises(ValueError):
            server._parse_date("2026-02-30")

    def test_find_period(self):
        first = NS(name="Trimestre 1")
        second = NS(name="Trimestre 2")
        client = NS(periods=[first, second], current_period=first)
        self.assertIs(server._find_period(client), first)
        self.assertIs(server._find_period(client, "trimestre 2"), second)
        self.assertIs(server._find_period(client, "2"), second)
        with self.assertRaises(ValueError):
            server._find_period(client, "unknown")

    def test_json_preserves_french_text(self):
        self.assertEqual(decode(server._json_result({"subject": "Français"})), {"subject": "Français"})


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock()
        self.client.info = NS(name="Fictional Student", class_name="Fictional Class", establishment="Fictional School")
        subject = NS(name="Français")
        self.period = NS(
            id="p1", name="Trimestre 1", start=date(2026, 9, 1), end=date(2026, 12, 18),
            overall_average="15", class_overall_average="13",
            grades=[NS(subject=subject, grade="16", out_of="20", date=date(2026, 10, 2))],
            averages=[NS(subject=subject, student="15", class_average="13", max="19", min="8", out_of="20")],
            absences=[NS(from_date=datetime(2026, 10, 1, 8), to_date=datetime(2026, 10, 1, 9),
                         justified=True, hours=1, days=0, reasons=["Fictional reason"])],
            delays=[NS(date=datetime(2026, 10, 2, 8), minutes=5, justified=False, reasons=[])],
            punishments=[],
        )
        self.client.current_period = self.period
        self.client.periods = [self.period]
        self.client.lessons.return_value = [NS(
            subject=subject, start=datetime(2026, 10, 5, 8), end=datetime(2026, 10, 5, 9),
            canceled=False, status=None, classroom="101", teacher_name="Fictional Teacher", memo=None,
        )]
        self.client.homework.return_value = [NS(
            subject=subject, description="Lire le chapitre", date=date(2026, 10, 6), done=False,
            background_color="#ffffff",
            files=[],
        )]
        food = NS(name="Riz", labels=[])
        self.client.menus.return_value = [NS(
            date=date(2026, 10, 5), name="Déjeuner", is_lunch=True, is_dinner=False,
            first_meal=[], main_meal=[food], side_meal=[], other_meal=[], cheese=[], dessert=[],
        )]
        self.config_patch = patch.object(server, "CONFIG", {"account_type": "student"})
        self.client_patch = patch.object(server, "_get_client", return_value=self.client)
        self.config_patch.start()
        self.client_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.client_patch.stop)

    async def test_tool_registry(self):
        tools = await server.list_tools()
        self.assertEqual({tool.name for tool in tools}, server.TOOL_HANDLERS)
        self.assertEqual(len(tools), 28)
        for tool in tools:
            self.assertFalse(tool.inputSchema["additionalProperties"])

    async def test_timetable(self):
        result = decode(await server.call_tool("get_timetable", {"date": "2026-10-05"}))
        self.assertEqual(result["lesson_count"], 1)
        self.assertEqual(result["lessons"][0]["teacher"], "Fictional Teacher")
        self.assertIsNone(result["child"])
        self.client.lessons.assert_called_once_with(date(2026, 10, 5), None)

    async def test_grades(self):
        result = decode(await server.call_tool("get_grades", {}))
        self.assertEqual(result["grade_count"], 1)
        self.assertEqual(result["grades"][0]["grade"], "16")
        self.assertEqual(result["overall_average"], "15")

    async def test_homework(self):
        result = decode(await server.call_tool("get_homework", {"date_from": "2026-10-05"}))
        self.assertEqual(result["homework_count"], 1)
        self.assertEqual(result["homework"][0]["description"], "Lire le chapitre")
        self.client.homework.assert_called_once_with(date(2026, 10, 5), date(2026, 10, 19))

    async def test_absences(self):
        result = decode(await server.call_tool("get_absences", {}))
        self.assertEqual(result["absence_count"], 1)
        self.assertEqual(result["delay_count"], 1)
        self.assertEqual(result["punishment_count"], 0)

    async def test_student_info(self):
        result = decode(await server.call_tool("get_student_info", {}))
        self.assertEqual(result["account_type"], "student")
        self.assertTrue(result["logged_in"])
        self.assertEqual(result["current_period"], "Trimestre 1")
        self.assertTrue(result["periods"][0]["is_current"])

    async def test_averages(self):
        result = decode(await server.call_tool("get_averages", {}))
        self.assertEqual(result["averages"][0]["student_average"], "15")

    async def test_menus(self):
        result = decode(await server.call_tool("get_menus", {"date_from": "2026-10-05"}))
        self.assertEqual(result["menu_count"], 1)
        self.assertEqual(result["menus"][0]["main_meal"][0]["name"], "Riz")

    async def test_unknown_tool_never_connects(self):
        with patch.object(server, "_get_client") as connect:
            result = decode(await server.call_tool("send_message", {}))
        self.assertIn("Unknown tool", result["error"])
        connect.assert_not_called()

    async def test_communication_dispatch(self):
        args = {"discussion_id": "fictional"}
        with patch.object(server.communication, "handle", return_value={"messages": []}) as handler:
            result = decode(await server.call_tool("get_discussion_messages", args))
        self.assertEqual(result, {"messages": []})
        handler.assert_called_once_with("get_discussion_messages", self.client, args, None)

    async def test_pedagogy_dispatch(self):
        with patch.object(server.pedagogy, "handle", return_value={"lesson_contents": []}) as handler:
            result = decode(await server.call_tool("get_lesson_contents", {}))
        self.assertEqual(result, {"lesson_contents": []})
        handler.assert_called_once_with("get_lesson_contents", self.client, {}, None)

    async def test_added_tools_are_read_only(self):
        for tool in server.communication.TOOLS + server.pedagogy.TOOLS:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)

    async def test_invalid_date(self):
        result = decode(await server.call_tool("get_timetable", {"date": "not-a-date"}))
        self.assertIn("error", result)
        self.client.lessons.assert_not_called()

    async def test_configuration_error(self):
        with patch.object(server, "_get_client", side_effect=ValueError("Missing credentials")):
            result = decode(await server.call_tool("get_student_info", {}))
        self.assertEqual(result["error"], "Missing credentials")

    async def test_authentication_error_clears_client(self):
        with patch.object(server, "_client", self.client), \
                patch.object(server, "_get_client", side_effect=server.pronotepy.CryptoError("Fictional failure")):
            result = decode(await server.call_tool("get_student_info", {}))
            self.assertIsNone(server._client)
        self.assertIn("Authentication error", result["error"])

    async def test_child_error_is_specific(self):
        with patch.object(server, "_get_client", side_effect=server.pronotepy.ChildNotFound("Unknown child")):
            result = decode(await server.call_tool("get_student_info", {}))
        self.assertEqual(result["error"], "Child not found: Unknown child")

    async def test_api_error(self):
        with patch.object(server, "_get_client", side_effect=server.pronotepy.PronoteAPIError("Fictional failure")):
            result = decode(await server.call_tool("get_student_info", {}))
        self.assertIn("Pronote API error", result["error"])


if __name__ == "__main__":
    unittest.main()
