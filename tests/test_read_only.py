"""Read-only tools with fictional fixtures matching the PRONOTE response shape."""

from datetime import date, datetime
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import communication
import pedagogy


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.discussion = Mock(id="d1", subject="Fictional subject", creator="Fictional Teacher",
                               unread=2, closed=False, labels=[])
        original = NS(id="m1", author="Fictional Teacher", seen=False, created=datetime(2026, 10, 1, 8),
                      content="Bonjour", replying_to=None)
        reply = NS(id="m2", author=None, seen=True, created=datetime(2026, 10, 1, 9),
                   content="Merci", replying_to=original)
        self.discussion.messages = [original, reply]
        self.client.discussions.return_value = [self.discussion]
        self.info = Mock(id="i1", title="Fictional notice", author="Fictional School", read=False,
                         creation_date=datetime(2026, 10, 1), start_date=datetime(2026, 10, 1),
                         end_date=None, category="Information", survey=False, anonymous_response=False)
        self.info.content.return_value = "Fictional announcement text"
        self.info.attachments.return_value = [NS(id="a1", name="notice.pdf", type=1, url="https://example.invalid/notice.pdf")]
        self.client.information_and_surveys.return_value = [self.info]

    def tearDown(self):
        self.discussion.mark_as.assert_not_called()
        self.info.mark_as_read.assert_not_called()
        self.client.new_discussion.assert_not_called()

    def test_list_discussions(self):
        result = communication.handle("get_discussions", self.client, {"only_unread": True}, None)
        self.assertEqual(result["discussions"][0]["unread"], 2)
        self.client.discussions.assert_called_once_with(only_unread=True)
        self.discussion.participants.assert_not_called()

    def test_read_messages(self):
        result = communication.handle("get_discussion_messages", self.client, {"discussion_id": "d1"}, None)
        self.assertEqual(result["messages"][1]["replying_to_id"], "m1")
        self.assertTrue(result["messages"][1]["sent_by_self"])

    def test_missing_discussion(self):
        with self.assertRaisesRegex(ValueError, "Discussion not found"):
            communication.handle("get_discussion_messages", self.client, {"discussion_id": "missing"}, None)

    def test_information_inclusive_dates(self):
        communication.handle("get_information_and_surveys", self.client, {"date_from": "2026-10-01", "date_to": "2026-10-04"}, None)
        self.client.information_and_surveys.assert_called_once_with(
            date_from=datetime(2026, 10, 1), date_to=datetime(2026, 10, 5), only_unread=False)
        self.info.content.assert_not_called()

    def test_announcement_content_and_attachments(self):
        result = communication.handle("get_information", self.client, {"information_id": "i1"}, None)
        self.assertEqual(result["information"]["content"], "Fictional announcement text")
        self.assertEqual(result["information"]["attachments"][0]["name"], "notice.pdf")

    def test_survey_details_are_not_silently_truncated(self):
        self.info.survey = True
        with self.assertRaisesRegex(ValueError, "Survey details are not supported"):
            communication.handle("get_information", self.client, {"information_id": "i1"}, None)
        self.info.content.assert_not_called()

    def test_pagination(self):
        selected, page = communication.paginate(list(range(5)), {"limit": 2, "offset": 2})
        self.assertEqual(selected, [2, 3])
        self.assertEqual(page["next_offset"], 4)
        self.assertEqual(page["total_count"], 5)
        self.assertIsNone(communication.paginate([1], {"offset": 5})[1]["next_offset"])

    def test_invalid_pagination(self):
        for args in ({"limit": True}, {"limit": 0}, {"limit": 201}, {"offset": -1}, {"offset": "0"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                communication.paginate([], args)


def typed(value):
    return {"V": value}


class PedagogyTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get_week.side_effect = lambda day: day.isocalendar().week
        self.client.communication.root_site = "https://example.invalid/pronote"
        self.client.communication.encryption.aes_encrypt.return_value = b"fictional"
        self.client.attributes = {"h": "fictional-session"}
        content = {"N": "c1", "descriptif": typed("<p>Fictional lesson text</p>"),
                   "categorie": typed({"L": "Cours"}),
                   "ListePieceJointe": typed([{"N": "a1", "G": 1, "L": "lesson.pdf"}])}
        self.response = {
            "ListeCahierDeTextes": typed([
                {"Date": typed("01/10/2026 08:00:00"), "DateFin": typed("01/10/2026 09:00:00"),
                 "Matiere": typed({"L": "Français"}), "cours": typed({"N": "lesson1"}),
                 "listeProfesseurs": typed([{"L": "Fictional Teacher"}]),
                 "listeContenus": typed([content, {**content, "N": "c2"}])},
                {"Date": typed("28/09/2026 08:00:00"), "Matiere": typed({"L": "Français"}),
                 "listeContenus": typed([content])},
            ]),
            "ListeRessourcesPedagogiques": typed({
                "listeMatieres": typed([{"N": "subject1", "L": "Français"}]),
                "listeRessources": typed([
                    {"G": 0, "date": typed("01/10/2026 08:00:00"), "matiere": typed({"N": "subject1"}),
                     "ressource": typed({"N": "a1", "G": 48, "L": "lesson.pdf"})},
                    {"G": 1, "date": typed("02/10/2026 08:00:00"), "matiere": typed({"N": "subject1"}),
                     "ressource": typed({"N": "a2", "G": 48, "L": "Fictional link", "url": "https://example.invalid/lesson"})},
                    {"G": 6, "date": typed("03/10/2026 08:00:00"), "matiere": typed({"N": "subject1"}),
                     "ressource": typed({"N": "a3", "L": "Fictional platform", "titre": "Platform",
                                         "description": typed("<p>Fictional platform description</p>"), "avecLien": True})},
                ]),
            }),
        }
        self.client.post.return_value = {"dataSec": {"data": self.response}}
        self.args = {"date_from": "2026-10-01", "date_to": "2026-10-04"}

    def test_all_contents_with_single_request(self):
        result = pedagogy.handle("get_lesson_contents", self.client, self.args, None)
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["lesson_contents"][0]["description"], "Fictional lesson text")
        self.assertEqual(result["lesson_contents"][0]["attachments"][0]["type"], 1)
        self.client.post.assert_called_once()
        self.assertEqual(self.client.post.call_args.args[:2], ("PageCahierDeTexte", 89))

    def test_subject_filter(self):
        result = pedagogy.handle("get_lesson_contents", self.client, {**self.args, "subject": "français"}, None)
        self.assertEqual(result["total_count"], 2)
        result = pedagogy.handle("get_lesson_contents", self.client, {**self.args, "subject": "Maths"}, None)
        self.assertEqual(result["total_count"], 0)

    def test_resources_have_distinct_genres(self):
        result = pedagogy.handle("get_teaching_resources", self.client, self.args, None)
        self.assertEqual(result["total_count"], 3)
        platform, link, file = result["teaching_resources"]
        self.assertEqual(platform["kind"], "platform")
        self.assertIsNone(platform["attachment"])
        self.assertEqual(link["attachment"]["type"], 0)
        self.assertEqual(link["attachment"]["url"], "https://example.invalid/lesson")
        self.assertEqual(file["attachment"]["type"], 1)
        self.assertIn("FichiersExternes", file["attachment"]["url"])

    def test_resource_pagination(self):
        result = pedagogy.handle("get_teaching_resources", self.client, {**self.args, "limit": 1}, None)
        self.assertEqual(len(result["teaching_resources"]), 1)
        self.assertEqual(result["next_offset"], 1)

    def test_invalid_date_range_never_requests(self):
        for args in ({"date_from": "2026-10-04", "date_to": "2026-10-01"},
                     {"date_from": "2026-01-01", "date_to": "2026-10-01"},
                     {"date_from": "bad"}, {**self.args, "limit": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                pedagogy.handle("get_lesson_contents", self.client, args, None)
        self.client.post.assert_not_called()

    def test_empty_cahier(self):
        self.client.post.return_value = {"dataSec": {"data": {}}}
        for name in ("get_lesson_contents", "get_teaching_resources"):
            self.assertEqual(pedagogy.handle(name, self.client, self.args, None)["total_count"], 0)
