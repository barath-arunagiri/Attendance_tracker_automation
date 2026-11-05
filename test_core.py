import json
import os
import tempfile
import unittest
from unittest.mock import patch

import core


class AttendanceMessageTests(unittest.TestCase):
    def test_attendance_kind_distinguishes_arrival_and_logout(self):
        examples = (
            ("Logged in at 9:05 AM", "Logged in"),
            ("Reached the office at 9:15", "Reached office"),
            ("I logged out", "Logged out"),
            ("Leaving the office now", "Logged out"),
            ("Can someone share the meeting notes?", ""),
        )
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertEqual(core.attendance_kind(text), expected)

    def test_sender_alias_matches_cleaned_whatsapp_ids(self):
        aliases = {"12345": "Asha"}

        self.assertEqual(core.sender_alias({"12345:4@s.whatsapp.net"}, aliases), "Asha")
        self.assertEqual(core.sender_alias({"hidden-id"}, aliases), "")

    def test_attendance_messages_round_trip_with_unicode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "attendance_messages.json")
            rows = [{
                "id": "message-1",
                "ts": 1793812200,
                "ids": ["12345"],
                "name": "Asha",
                "text": "Reached office ✅",
                "kind": "Reached office",
                "result": "Pending tracker handling",
                "group": "group-id",
            }]
            with patch.object(core, "ATTENDANCE_MESSAGES_FILE", path):
                core.save_attendance_messages(rows)
                self.assertEqual(core.load_attendance_messages(), rows)
                with open(path, encoding="utf-8") as file:
                    self.assertIn("✅", json.load(file)[0]["text"])

    def test_corrupt_saved_messages_are_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "attendance_messages.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write("not json")
            with patch.object(core, "ATTENDANCE_MESSAGES_FILE", path):
                with self.assertRaises(json.JSONDecodeError):
                    core.load_attendance_messages()


if __name__ == "__main__":
    unittest.main()
