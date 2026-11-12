import json
import os
import tempfile
import unittest
from datetime import datetime
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

    def test_yesterdays_linked_login_event_produces_tracker_operation(self):
        day = datetime(2026, 11, 10).date()
        sent = datetime(2026, 11, 10, 9, 5, tzinfo=core.IST)
        ops, rows = core.plan_fill(
            [{"date": day, "name": "Dharanidha", "status": ""}],
            [{"name": "Dharanidha", "dt": sent, "text": "Logged in"}],
            {"Dharanidha": "9:00 AM"},
            today=datetime(2026, 11, 11).date(),
        )

        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["kind"], "in")
        self.assertEqual(ops[0]["name"], "Dharanidha")
        self.assertEqual(ops[0]["reported"], "9:05 AM")
        self.assertEqual(rows[0][2], "9:05 AM  Late")

    def test_attendance_report_groups_types_and_preserves_export_names(self):
        day = datetime(2026, 11, 10)
        messages = [
            {"name": "Name Saved in WhatsApp", "dt": day.replace(hour=17, minute=30, tzinfo=core.IST),
             "text": "Logged out"},
            {"name": "Name Saved in WhatsApp", "dt": day.replace(hour=9, minute=15, tzinfo=core.IST),
             "text": "Reached office"},
            {"name": "Name Saved in WhatsApp", "dt": day.replace(hour=9, minute=5, tzinfo=core.IST),
             "text": "Logged in"},
            {"name": "Name Saved in WhatsApp", "dt": day.replace(hour=9, minute=5, tzinfo=core.IST),
             "text": "Logged in"},
            {"name": "Other day", "dt": datetime(2026, 11, 11, 9, tzinfo=core.IST), "text": "Logged in"},
        ]

        report = core.build_attendance_report(messages, day.date(), day.date())

        self.assertIn("Logged in:\n  9:05 AM | Name Saved in WhatsApp | Logged in", report)
        self.assertIn("Reached office:\n  9:15 AM | Name Saved in WhatsApp | Reached office", report)
        self.assertIn("Logged out:\n  5:30 PM | Name Saved in WhatsApp | Logged out", report)
        self.assertNotIn("Other day", report)
        self.assertEqual(report.count("9:05 AM | Name Saved in WhatsApp | Logged in"), 1)

    def test_attendance_report_uses_saved_app_category_and_display_name(self):
        day = datetime(2026, 11, 10)
        messages = [{
            "name": "Dharanidha",
            "dt": day.replace(hour=10, minute=37, tzinfo=core.IST),
            "text": "Logged In",
            "kind": "Logged in",
        }]

        report = core.build_attendance_report(messages, day.date(), day.date())

        self.assertIn("10:37 AM | Dharanidha | Logged In", report)

    def test_exact_duplicate_attendance_delivery_is_removed(self):
        message = {
            "id": "message-1",
            "ts": 1794340000,
            "ids": ["12345"],
            "name": "Praveen Bro DP",
            "text": "Logged out",
            "kind": "Logged out",
            "result": "Display only",
            "group": "group-id",
        }
        duplicate = dict(message, id="message-2")

        rows, changed = core.dedupe_attendance_messages([message, duplicate])

        self.assertTrue(changed)
        self.assertEqual(len(rows), 1)

    def test_near_matching_sender_names_and_distinct_ids_are_not_deduplicated(self):
        base = {
            "ts": 1794340000,
            "ids": ["12345"],
            "name": "Praveen Bro DP",
            "text": "Logged out",
            "kind": "Logged out",
            "group": "group-id",
        }
        different_full_name = dict(base, id="message-2", name="Praveen Kumar Bro DP")
        different_sender = dict(base, id="message-3", ids=["67890"])

        rows, changed = core.dedupe_attendance_messages([
            dict(base, id="message-1"),
            different_full_name,
            different_sender,
        ])

        self.assertFalse(changed)
        self.assertEqual(len(rows), 3)

    def test_exact_name_and_message_deduplicates_when_sender_id_is_missing(self):
        message = {
            "id": "message-1",
            "ts": 1794340000,
            "ids": [],
            "name": "Praveen Bro DP",
            "text": "Logged out",
            "kind": "Logged out",
            "group": "group-id",
        }
        duplicate = dict(message, id="message-2")

        rows, changed = core.dedupe_attendance_messages([message, duplicate])

        self.assertTrue(changed)
        self.assertEqual(len(rows), 1)

    def test_missing_id_messages_with_different_full_names_remain_separate(self):
        first = {
            "id": "message-1",
            "ts": 1794340000,
            "ids": [],
            "name": "Praveen Bro DP",
            "text": "Logged out",
            "group": "group-id",
        }
        second = dict(first, id="message-2", name="Praveen Kumar Bro DP")

        rows, changed = core.dedupe_attendance_messages([first, second])

        self.assertFalse(changed)
        self.assertEqual(len(rows), 2)

    def test_missing_id_messages_without_names_are_not_assumed_to_be_same_sender(self):
        message = {
            "id": "message-1",
            "ts": 1794340000,
            "ids": [],
            "name": "",
            "text": "Logged out",
            "group": "group-id",
        }
        duplicate = dict(message, id="message-2")

        rows, changed = core.dedupe_attendance_messages([message, duplicate])

        self.assertFalse(changed)
        self.assertEqual(len(rows), 2)

    def test_sender_ids_can_be_restored_to_a_saved_message(self):
        entry = {"ids": []}

        self.assertTrue(core.merge_attendance_sender_ids(entry, {"919397009717@s.whatsapp.net"}))
        self.assertEqual(entry["ids"], ["919397009717"])
        self.assertFalse(core.merge_attendance_sender_ids(entry, {"919397009717"}))

    def test_daily_attendance_groups_sender_ids_not_shared_employee_links(self):
        day = datetime(2026, 11, 11, tzinfo=core.IST)
        first = {
            "id": "message-1",
            "ts": day.replace(hour=10, minute=37).timestamp(),
            "group": "group-id",
            "ids": ["240415122976965"],
            "kind": "Reached office",
            "text": "Reached office",
        }
        second = {
            "id": "message-2",
            "ts": day.replace(hour=11, minute=16).timestamp(),
            "group": "group-id",
            "ids": ["197903737962735"],
            "kind": "Logged in",
            "text": "Logged in",
        }
        same_sender = dict(
            second,
            id="message-3",
            ts=day.replace(hour=11, minute=30).timestamp(),
            ids=["240415122976965"],
        )
        another_day = dict(
            second,
            id="message-4",
            ts=day.replace(day=12, hour=9).timestamp(),
        )

        groups = core.group_daily_attendance_entries(
            [second, first, another_day, same_sender],
            lambda row: ("sender", tuple(row["ids"])),
        )

        self.assertEqual([[row["id"] for row in group] for group in groups], [
            ["message-1", "message-3"],
            ["message-2"],
            ["message-4"],
        ])

    def test_whatsapp_phone_and_lid_for_same_sender_are_expanded(self):
        ids = core.expand_ids(
            {"197903737962735"},
            [{"197903737962735", "919360399422"}],
        )

        self.assertEqual(ids, {"197903737962735", "919360399422"})

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
