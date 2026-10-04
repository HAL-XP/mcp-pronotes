"""Additional tools with fictional data. No live account mutations."""

from datetime import date
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, Mock, patch

import features
import server


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.period = NS(name="Fictional Period", report=None, evaluations=[])
        self.find_period = Mock(return_value=self.period)
        self.discussion = Mock(id="d1", closed=False)
        self.message = Mock(id="m1")
        self.discussion.messages = [self.message]
        self.client.discussions.return_value = [self.discussion]
        self.info = Mock(id="i1", survey=False)
        self.client.information_and_surveys.return_value = [self.info]
        self.hw = Mock(id="h1")
        self.client.homework.return_value = [self.hw]
        self.client.get_recipients.return_value = [NS(id="r1", name="Fictional Teacher", type="teacher")]
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def call(self, name, args=None):
        return features.handle(name, self.client, args or {}, None, self.find_period)

    def action(self, name, args):
        with patch.dict(os.environ, {"PRONOTE_ENABLE_WRITES": "1"}):
            return self.call(name, {**args, "confirm": True})

    def test_unpublished_report(self):
        self.assertFalse(self.call("get_report_card")["available"])

    def test_published_report(self):
        subject = NS(id="s1", name="French", comments=["Fictional comment"], student_average="15",
                     class_average="13", min_average="5", max_average="19", coefficient="1", teachers=[])
        self.period.report = NS(comments=["Fictional overall comment"], subjects=[subject])
        self.assertEqual(self.call("get_report_card")["report"]["subjects"][0]["student_average"], "15")

    def test_evaluations(self):
        acquisition = Mock()
        acquisition.to_dict.return_value = {"level": "Acquis"}
        self.period.evaluations = [NS(id="e1", name="Reading", domain="French", teacher="Fictional Teacher",
                                     coefficient=1, description="Fictional", subject=NS(name="French"),
                                     date=date(2026, 10, 1), paliers=[], acquisitions=[acquisition])]
        self.assertEqual(self.call("get_evaluations")["evaluations"][0]["acquisitions"], [{"level": "Acquis"}])

    def test_teaching_staff_unavailable(self):
        self.client.post.return_value = {"dataSec": {"data": {}}}
        self.assertFalse(self.call("get_teaching_staff")["available"])

    def test_recipients(self):
        self.assertEqual(self.call("get_recipients")["recipients"][0]["id"], "r1")

    def test_discussion_participants(self):
        self.discussion.participants.return_value = ["Fictional Teacher"]
        self.assertEqual(self.call("get_discussion_participants", {"discussion_id": "d1"})["participants"], ["Fictional Teacher"])

    def test_message_recipients(self):
        self.message.recipients.return_value = ["Fictional Student"]
        self.assertEqual(self.call("get_message_recipients", {"discussion_id": "d1", "message_id": "m1"})["recipients"], ["Fictional Student"])

    def test_calendar(self):
        self.client.export_ical.return_value = "https://example.invalid/calendar.ics"
        self.assertTrue(self.call("export_calendar")["private"])

    def test_calendar_unavailable(self):
        self.client.export_ical.side_effect = features.pronotepy.ParsingError("Fictional unavailable feed", {}, ("iCal", "liste", "V"))
        self.assertFalse(self.call("export_calendar")["available"])

    def test_timetable_pdf(self):
        self.client.generate_timetable_pdf.return_value = "https://example.invalid/timetable.pdf"
        self.call("export_timetable_pdf", {"date": "2026-10-01", "portrait": True})
        self.client.generate_timetable_pdf.assert_called_once_with(day=date(2026, 10, 1), portrait=True)

    def test_write_tools_disabled_by_default(self):
        for name in features.WRITES:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "disabled"):
                self.call(name, {"confirm": True})
        self.client.new_discussion.assert_not_called()

    def test_confirmation_required(self):
        with patch.dict(os.environ, {"PRONOTE_ENABLE_WRITES": "1"}):
            for confirm in (False, None, 1, "true"):
                with self.subTest(confirm=confirm), self.assertRaisesRegex(ValueError, "approval"):
                    self.call("create_discussion", {"confirm": confirm})

    def test_homework_action(self):
        self.action("set_homework_done", {"homework_id": "h1", "done": True})
        self.hw.set_done.assert_called_once_with(True)

    def test_send_action(self):
        self.action("create_discussion", {"subject": "Fictional", "message": "Fictional message", "recipient_ids": ["r1"]})
        self.client.new_discussion.assert_called_once()

    def test_invalid_recipient_never_sends(self):
        for ids in ([], ["missing"], ["r1", "r1"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.action("create_discussion", {"subject": "Fictional", "message": "Fictional", "recipient_ids": ids})
        self.client.new_discussion.assert_not_called()

    def test_reply_action(self):
        self.action("reply_to_message", {"discussion_id": "d1", "message_id": "m1", "message": "Fictional reply"})
        self.message.reply.assert_called_once_with("Fictional reply")

    def test_closed_discussion_never_replies(self):
        self.discussion.closed = True
        with self.assertRaisesRegex(ValueError, "closed"):
            self.action("reply_to_message", {"discussion_id": "d1", "message_id": "m1", "message": "Fictional"})
        self.message.reply.assert_not_called()

    def test_mark_discussion(self):
        self.action("mark_discussion_read", {"discussion_id": "d1", "read": False})
        self.discussion.mark_as.assert_called_once_with(False)

    def test_mark_information(self):
        self.action("mark_information_read", {"information_id": "i1", "read": True})
        self.info.mark_as_read.assert_called_once_with(True)

    def test_survey_cannot_be_acknowledged(self):
        self.info.survey = True
        with self.assertRaisesRegex(ValueError, "Survey acknowledgment"):
            self.action("mark_information_read", {"information_id": "i1", "read": True})
        self.info.mark_as_read.assert_not_called()

    def test_survey_reads_all_questions(self):
        self.info.survey = True
        self.info.title = "Fictional survey"
        self.info._raw_content = [{"N": "q1", "texte": {"V": "<p>First question</p>"}},
                                  {"N": "q2", "texte": {"V": "<p>Second question</p>"}}]
        self.assertEqual(len(self.call("get_survey", {"information_id": "i1"})["questions"]), 2)
        self.info.mark_as_read.assert_not_called()

    def test_mutations_never_use_retrying_post(self):
        original = self.client.post
        self.client.communication.post.side_effect = RuntimeError("Fictional ambiguous network failure")
        with self.assertRaises(RuntimeError), features.no_write_retry(self.client):
            self.client.post("SaisieMessage", 131, {"contenu": "Fictional"})
        original.assert_not_called()
        self.client.communication.post.assert_called_once()
        self.assertIs(self.client.post, original)

    def test_parent_write_keeps_selected_child_signature(self):
        client = Mock(spec=features.pronotepy.ParentClient)
        client._selected_child = NS(id="fictional-child")
        client.communication = Mock()
        with features.no_write_retry(client):
            client.post("SaisieTAFFaitEleve", 88, {"listeTAF": []})
        self.assertEqual(client.communication.post.call_args.args[1]["Signature"]["membre"], {"N": "fictional-child", "G": 4})


class DownloadTests(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        self.temp = tempfile.TemporaryDirectory(dir=preferred if preferred.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "downloads"
        self.client = Mock()
        self.client.communication.root_site = "https://example.invalid/pronote"
        self.file = {"id": "a1", "name": "../../fictional.pdf", "type": 1,
                     "url": "https://example.invalid/pronote/FichiersExternes/fake/file.pdf"}
        resolver = patch.object(features, "resolve_attachment", return_value=self.file)
        resolver.start()
        self.addCleanup(resolver.stop)
        directory = patch.object(features, "DOWNLOAD_DIRECTORY", self.directory)
        directory.start()
        self.addCleanup(directory.stop)
        self.response = Mock(status_code=200, headers={"Content-Type": "application/pdf"})
        self.response.iter_content.return_value = iter([b"fictional PDF"])
        context = MagicMock()
        context.__enter__.return_value = self.response
        self.client.communication.session.get.return_value = context

    def test_private_download(self):
        result = features.download(self.client, {})
        path = Path(result["path"])
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(path.read_bytes(), b"fictional PDF")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.client.communication.session.get.call_args.kwargs["allow_redirects"])

    def test_external_url_never_requested(self):
        self.file["url"] = "https://other.invalid/file.pdf"
        with self.assertRaises(ValueError):
            features.download(self.client, {})
        self.client.communication.session.get.assert_not_called()

    def test_redirect_and_login_html_rejected(self):
        for status, content in ((302, "application/pdf"), (200, "text/html")):
            self.response.status_code = status
            self.response.headers["Content-Type"] = content
            with self.assertRaises(ValueError):
                features.download(self.client, {})

    def test_size_limit_and_partial_cleanup(self):
        self.response.iter_content.return_value = iter([b"a" * (20 * 1024 * 1024 + 1)])
        with self.assertRaises(ValueError):
            features.download(self.client, {})
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_symlink_directory_rejected(self):
        self.directory.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            features.download(self.client, {})
        self.client.communication.session.get.assert_not_called()


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_action_never_connects(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(server, "_get_client") as connect:
            result = await server.call_tool("create_discussion", {"confirm": True})
        self.assertIn("disabled", result[0].text)
        connect.assert_not_called()
