import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

import tracker
from core import IST


class OpenpyxlTestTracker(tracker.Tracker):
    def _save(self, workbook):
        workbook.save(self.path)
        return None


class TrackerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = os.path.join(self.temp_dir.name, "tracker.xlsx")
        self.today = datetime.now(IST).date()
        self.yesterday = self.today - timedelta(days=1)
        self._create_workbook()
        self.patches = (
            patch.object(tracker, "APP_DIR", self.temp_dir.name),
            patch.object(tracker, "BACKUPS", os.path.join(self.temp_dir.name, "backups")),
        )
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def _create_workbook(self):
        workbook = Workbook()
        sheet_by_month = {}
        for day in (self.yesterday, self.today):
            title = day.strftime("%b - %y")
            worksheet = sheet_by_month.get(title)
            if worksheet is None:
                worksheet = workbook.active if not sheet_by_month else workbook.create_sheet()
                worksheet.title = title
                for column, value in enumerate(
                    ("date", "day", "name", "time", "reported", "status"), start=1
                ):
                    worksheet.cell(1, column).value = value
                sheet_by_month[title] = worksheet
            row = worksheet.max_row + 1
            worksheet.cell(row, 1).value = datetime.combine(day, datetime.min.time())
            worksheet.cell(row, 2).value = day.strftime("%A")
            worksheet.cell(row, 3).value = "Alex"
            worksheet.cell(row, 4).value = "9:00 AM"
        workbook.save(self.path)
        workbook.close()

    def _login_ops(self):
        return [
            {
                "kind": "in",
                "date": day,
                "name": "Alex",
                "reported": f"9:{minute:02d} AM",
                "status": "On-time",
            }
            for day, minute in ((self.yesterday, 5), (self.today, 10))
        ]

    def _openpyxl_tracker(self):
        return OpenpyxlTestTracker(self.path)

    def test_login_times_for_today_and_yesterday_are_persisted(self):
        result = self._openpyxl_tracker().update(self._login_ops())

        self.assertEqual(result, [("written", ""), ("written", "")])
        workbook = load_workbook(self.path, data_only=True)
        try:
            for day, expected in ((self.yesterday, "9:05 AM"), (self.today, "9:10 AM")):
                worksheet = workbook[day.strftime("%b - %y")]
                row = next(
                    row for row in range(2, worksheet.max_row + 1)
                    if worksheet.cell(row, 1).value.date() == day
                )
                self.assertEqual(worksheet.cell(row, 5).value, expected)
        finally:
            workbook.close()

    def test_silent_save_failure_is_reported_as_error(self):
        class NoSaveTracker(OpenpyxlTestTracker):
            def _save(self, workbook):
                return None

        result = NoSaveTracker(self.path).update(self._login_ops())

        self.assertEqual(len(result), 2)
        for state, detail in result:
            self.assertEqual(state, "error")
            self.assertIn("read-back mismatch", detail)

    def test_existing_login_time_is_not_overwritten(self):
        workbook = load_workbook(self.path)
        worksheet = workbook[self.today.strftime("%b - %y")]
        row = next(
            row for row in range(2, worksheet.max_row + 1)
            if worksheet.cell(row, 1).value.date() == self.today
        )
        worksheet.cell(row, 5).value = "9:12 AM"
        workbook.save(self.path)
        workbook.close()

        result = self._openpyxl_tracker().update([self._login_ops()[1]])

        self.assertEqual(result, [("skipped", "already 9:12 AM")])
        workbook = load_workbook(self.path, data_only=True)
        try:
            self.assertEqual(workbook[self.today.strftime("%b - %y")].cell(row, 5).value, "9:12 AM")
        finally:
            workbook.close()


if __name__ == "__main__":
    unittest.main()
