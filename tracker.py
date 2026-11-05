"""Reads/writes the Login & Leave tracker. Touches ONLY the Reported and Status
cells of the matched employee on the matched date."""
import glob, os, re, shutil
import calendar
from collections import Counter
from copy import copy
from datetime import datetime, date, timedelta
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from core import APP_DIR, IST, norm

HEAD = ["date", "day", "name", "time", "reported", "status"]
FILLS = {"On-time": "C6EFCE", "Late": "FFC7CE", "CL": "FCE4D6"}
BACKUPS = os.path.join(APP_DIR, "backups")


def _key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def parse_date(v, year):
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    s = str(v).strip()
    for f in ("%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d %b %Y", "%d %b %y", "%Y-%m-%d"):
        try: return datetime.strptime(s, f).date()
        except ValueError: pass
    for f in ("%d-%b", "%d %b", "%d/%m"):
        try: return datetime.strptime(f"{s} {year}", f + " %Y").date()
        except ValueError: pass
    return None


def find_blocks(ws):
    """Every 'Date|Day|Name|Time|Reported|Status' header (WEEK blocks) -> (row, col)."""
    found = []
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 25)):
        for c in row:
            if norm(c.value) == "date" and [norm(ws.cell(c.row, c.column + i).value) for i in range(6)] == HEAD:
                found.append((c.row, c.column))
    return found


def walk(ws, hdr_row, col, year):
    """Yield (row, date) - merged Date cells carry forward from their top-left value."""
    cur = None
    for r in range(hdr_row + 1, ws.max_row + 1):
        v = ws.cell(r, col).value
        if v not in (None, ""):
            cur = parse_date(v, year)
        yield r, cur


def status_fill(ws, status, cache):
    """Use the tracker sheet's most common fill for this status, with a legacy fallback."""
    key = (ws.title, status)
    if key not in cache:
        matches = []
        for hdr_row, col in find_blocks(ws):
            for row in range(hdr_row + 1, ws.max_row + 1):
                if norm(ws.cell(row, col + 5).value) == norm(status):
                    fill = ws.cell(row, col + 5).fill
                    color = fill.fgColor
                    signature = (fill.fill_type, color.type, color.rgb, color.indexed, color.theme, color.tint)
                    matches.append((signature, fill))
        if matches:
            signature, _ = Counter(item[0] for item in matches).most_common(1)[0]
            cache[key] = copy(next(fill for sig, fill in matches if sig == signature))
        else:
            cache[key] = PatternFill("solid", start_color=FILLS[status], end_color=FILLS[status])
    return copy(cache[key])


def _common_alignment(samples):
    if not samples:
        return None
    signatures = Counter(
        (a.horizontal, a.vertical, a.text_rotation, a.wrap_text, a.shrink_to_fit,
         a.indent, a.relativeIndent, a.justifyLastLine, a.readingOrder)
        for a in samples
    )
    signature = signatures.most_common(1)[0][0]
    return copy(next(
        alignment for alignment in samples
        if (alignment.horizontal, alignment.vertical, alignment.text_rotation, alignment.wrap_text,
            alignment.shrink_to_fit, alignment.indent, alignment.relativeIndent,
            alignment.justifyLastLine, alignment.readingOrder) == signature
    ))


def status_alignment(ws, status, cache):
    """Use the common alignment for this status, or the sheet's common status alignment."""
    key = (ws.title, status)
    if key not in cache:
        matching, all_statuses = [], []
        for hdr_row, col in find_blocks(ws):
            for row in range(hdr_row + 1, ws.max_row + 1):
                cell = ws.cell(row, col + 5)
                if cell.value in (None, ""):
                    continue
                all_statuses.append(cell.alignment)
                if norm(cell.value) == norm(status):
                    matching.append(cell.alignment)
        cache[key] = _common_alignment(matching or all_statuses)
    return copy(cache[key]) if cache[key] is not None else None


def reported_alignment(ws, col, year, cache):
    """Use the most common alignment of reported times in this week block, then the month."""
    key = (ws.title, col)
    if key not in cache:
        blocks = find_blocks(ws)
        samples = []
        for hdr_row, block_col in blocks:
            if block_col != col:
                continue
            samples = [
                ws.cell(row, block_col + 4).alignment
                for row, _ in walk(ws, hdr_row, block_col, year)
                if ws.cell(row, block_col + 4).value not in (None, "")
                and norm(ws.cell(row, block_col + 4).value) not in {"cl", "holiday", "leave", "weekend"}
            ]
            break
        if not samples:
            samples = [
                ws.cell(row, block_col + 4).alignment
                for hdr_row, block_col in blocks
                for row, _ in walk(ws, hdr_row, block_col, year)
                if ws.cell(row, block_col + 4).value not in (None, "")
                and norm(ws.cell(row, block_col + 4).value) not in {"cl", "holiday", "leave", "weekend"}
            ]
        cache[key] = _common_alignment(samples)
    return copy(cache[key]) if cache[key] is not None else None


LOCKED = ("The tracker file is locked: it is open in Excel (or another program) on this laptop.\n"
          "Close it there - viewing it in Teams / Excel Online is fine - then try again.")


class Tracker:
    def __init__(self, path):
        self.path = path

    @staticmethod
    def _snapshot(wb):
        return {
            ws.title: {
                cell.coordinate: (cell.value, copy(cell._style))
                for cell in ws._cells.values()
            }
            for ws in wb.worksheets
        }

    def _load(self):
        """Open a COPY, so reading never fights with Excel or OneDrive."""
        tmp = os.path.join(APP_DIR, "_read.xlsx")
        try:
            shutil.copyfile(self.path, tmp)
        except PermissionError:
            raise PermissionError(LOCKED)
        wb = load_workbook(tmp)
        wb._tracker_baseline = self._snapshot(wb)
        return wb

    def _sheet(self, wb, d):
        """Month tab, tolerant of 'Oct - 26', 'Oct-26', 'Oct 2026', 'October 2026', 'Sept 26'."""
        fmts = ("%b - %y", "%b - %Y", "%B - %y", "%B - %Y")
        wants = {_key(d.strftime(f)) for f in fmts}
        if d.month == 9: wants |= {"sept" + d.strftime("%y"), "sept" + d.strftime("%Y")}
        for n in wb.sheetnames:
            if _key(n) in wants: return wb[n]
        return None

    def ensure_month(self, month_date, employees):
        """Create a missing month tab from the latest prior month using real weekdays."""
        wb = self._load()
        try:
            existing = self._sheet(wb, month_date)
            if existing is not None:
                if self._clear_empty_attendance_fills(existing, month_date):
                    error = self._save(wb)
                    if error:
                        raise OSError(error)
                return False

            source = None
            probe = month_date.replace(day=1)
            for _ in range(60):
                probe = (probe - timedelta(days=1)).replace(day=1)
                candidate = self._sheet(wb, probe)
                if candidate is not None and find_blocks(candidate):
                    source = candidate
                    break
            if source is None:
                raise ValueError(f"Cannot create '{month_date:%b - %y}': no earlier monthly tracker tab is available as a template.")

            source_blocks = find_blocks(source)
            header_row, first_col = source_blocks[0]
            source_starts = self._day_group_starts(source, header_row, first_col)
            if not source_starts:
                raise ValueError(f"Cannot create '{month_date:%b - %y}': template '{source.title}' has no weekday rows.")

            group_size = source_starts[0][1] - source_starts[0][0] + 1
            if any(end - start + 1 != group_size for start, end in source_starts):
                raise ValueError(f"Cannot create '{month_date:%b - %y}': template weekday rows have inconsistent employee counts.")
            if employees and len(employees) != group_size:
                raise ValueError(
                    f"Cannot create '{month_date:%b - %y}': template has {group_size} employee rows per day, "
                    f"but {len(employees)} employees are linked. Update the tracker template first."
                )

            sheet_name = month_date.strftime("%b - %y")
            source_index = wb.index(source)
            ws = wb.copy_worksheet(source)
            wb.move_sheet(ws, offset=source_index - wb.index(ws))
            ws.title = sheet_name
            first_day = month_date.replace(day=1)
            last_day = month_date.replace(day=calendar.monthrange(month_date.year, month_date.month)[1])
            monday = first_day - timedelta(days=first_day.weekday())
            week_count = ((last_day - monday).days // 7) + 1
            stride = (source_blocks[1][1] - source_blocks[0][1]) if len(source_blocks) > 1 else 7
            blocks = list(source_blocks)
            while len(blocks) < week_count:
                new_col = blocks[-1][1] + stride
                self._copy_week_block(ws, blocks[-1][1], new_col, header_row, stride, source_starts)
                blocks.append((header_row, new_col))
            for _, col in blocks:
                self._reset_merged_attendance_cells(ws, col, source_starts)
            for block_index, (_, col) in enumerate(blocks):
                starts = source_starts
                if len(starts) < 5:
                    raise ValueError(f"Cannot create '{sheet_name}': template does not contain five weekday rows per week.")
                ws.cell(2, col).value = f"{month_date:%B - %Y}  (WEEK - {block_index + 1})"
                for weekday_index, (row_start, row_end) in enumerate(starts[:5]):
                    day = monday + timedelta(days=7 * block_index + weekday_index)
                    date_cell, day_cell = ws.cell(row_start, col), ws.cell(row_start, col + 1)
                    date_cell.value = datetime.combine(day, datetime.min.time())
                    day_cell.value = day.strftime("%A")
                    for employee_index in range(group_size):
                        row = row_start + employee_index
                        if employees:
                            employee = employees[employee_index]
                            ws.cell(row, col + 2).value = employee["name"]
                            ws.cell(row, col + 3).value = employee["timing"]
                        reported, status = ws.cell(row, col + 4), ws.cell(row, col + 5)
                        reported.value = status.value = None
                        reported.fill = status.fill = PatternFill()
                    if row_end - row_start + 1 != group_size:
                        raise ValueError(f"Cannot create '{sheet_name}': weekday row group size changed in the template.")

            error = self._save(wb)
            if error:
                raise OSError(error)
            return True
        finally:
            wb.close()

    @staticmethod
    def _clear_empty_attendance_fills(ws, month_date):
        changed = False
        empty_fill = PatternFill()
        for header_row, col in find_blocks(ws):
            for row, day in walk(ws, header_row, col, month_date.year):
                if day is None or day.month != month_date.month or day.year != month_date.year:
                    continue
                for offset in (4, 5):
                    cell = ws.cell(row, col + offset)
                    if cell.value in (None, "") and cell.fill.fill_type is not None:
                        cell.fill = copy(empty_fill)
                        changed = True
        return changed

    @staticmethod
    def _reset_merged_attendance_cells(ws, col, groups):
        merged_groups = {}
        for index, (row_start, row_end) in enumerate(groups):
            merged_groups[index] = []
            for merged in list(ws.merged_cells.ranges):
                overlaps = (merged.min_col <= col + 5 and merged.max_col >= col + 4
                            and merged.min_row <= row_end and merged.max_row >= row_start)
                if not overlaps:
                    continue
                contained = (merged.min_col >= col + 4 and merged.max_col <= col + 5
                             and merged.min_row >= row_start and merged.max_row <= row_end)
                if not contained:
                    raise ValueError(
                        f"Cannot create this month: merged attendance range '{merged}' "
                        "crosses an employee-day boundary."
                    )
                merged_groups[index].append(str(merged))

        affected = {index for index, ranges in merged_groups.items() if ranges}
        if not affected:
            return
        reference_index = next((index for index in range(len(groups)) if index not in affected), None)
        if reference_index is None:
            raise ValueError("Cannot create this month: every weekday attendance block is merged.")

        for index in affected:
            for merged in merged_groups[index]:
                ws.unmerge_cells(merged)
            row_start, row_end = groups[index]
            reference_start, reference_end = groups[reference_index]
            if row_end - row_start != reference_end - reference_start:
                raise ValueError("Cannot create this month: merged attendance rows do not match the template.")
            for employee_index in range(row_end - row_start + 1):
                for offset in (4, 5):
                    source = ws.cell(reference_start + employee_index, col + offset)
                    target = ws.cell(row_start + employee_index, col + offset)
                    target._style = copy(source._style)
                    target.number_format = source.number_format

    @staticmethod
    def _day_group_starts(ws, header_row, col):
        groups = sorted(
            (merged.min_row, merged.max_row)
            for merged in ws.merged_cells.ranges
            if merged.min_col == col and merged.max_col == col and merged.min_row > header_row
        )
        if groups:
            return groups
        starts = []
        for row in range(header_row + 1, ws.max_row + 1):
            day = norm(ws.cell(row, col + 1).value)
            if day in {"monday", "tuesday", "wednesday", "thursday", "friday", "mon", "tue", "wed", "thu", "fri"}:
                end = row
                while end + 1 <= ws.max_row and ws.cell(end + 1, col + 2).value not in (None, ""):
                    end += 1
                starts.append((row, end))
        return starts

    @staticmethod
    def _copy_week_block(ws, source_col, target_col, header_row, stride, groups):
        delta = target_col - source_col
        max_row = max(header_row, max(end for _, end in groups) + 3)
        for row in range(1, max_row + 1):
            for offset in range(stride):
                source = ws.cell(row, source_col + offset)
                target = ws.cell(row, target_col + offset)
                if source.has_style:
                    target._style = copy(source._style)
                if source.number_format:
                    target.number_format = source.number_format
                if source.value is not None:
                    target.value = source.value
                if source.hyperlink:
                    target._hyperlink = copy(source.hyperlink)
                if source.comment:
                    target.comment = copy(source.comment)
        for offset in range(stride):
            source_letter = ws.cell(1, source_col + offset).column_letter
            target_letter = ws.cell(1, target_col + offset).column_letter
            source_dimension = ws.column_dimensions[source_letter]
            if source_dimension.width is not None:
                ws.column_dimensions[target_letter].width = source_dimension.width
            ws.column_dimensions[target_letter].hidden = source_dimension.hidden
        merges = [
            str(merged) for merged in ws.merged_cells.ranges
            if merged.min_col >= source_col and merged.max_col < source_col + stride
        ]
        for merged in merges:
            from openpyxl.utils.cell import range_boundaries, get_column_letter
            min_col, min_row, max_col, max_row = range_boundaries(merged)
            ws.merge_cells(
                f"{get_column_letter(min_col + delta)}{min_row}:"
                f"{get_column_letter(max_col + delta)}{max_row}"
            )
        ws.cell(2, target_col).value = ws.cell(2, source_col).value

    def names(self, for_date=None):
        """Employee names from the CURRENT calendar month's tab only (e.g. 'Oct - 26')."""
        d = for_date or datetime.now(IST).date()
        wb = self._load()
        try:
            ws = self._sheet(wb, d)
            if ws is None:
                raise ValueError(f"No tab for the current month ('{d:%b - %y}') in this file.\n\nTabs found: {', '.join(wb.sheetnames)}\n\nFile: {self.path}")
            out = []
            for hr, col in find_blocks(ws):
                for r in range(hr + 1, ws.max_row + 1):
                    v = ws.cell(r, col + 2).value
                    if isinstance(v, str) and v.strip() and v.strip() not in out:
                        out.append(v.strip())
            return out
        finally:
            wb.close()

    def empties(self, d_from, d_to, names=None):
        """Weekday rows between d_from and d_to whose Reported is blank and Status is blank or CL.
        -> (list of {date, name, status}, [months with no tab])"""
        wb = self._load()
        try:
            want = {norm(n) for n in names} if names else None
            out, seen, missing, months, d = [], set(), [], [], d_from.replace(day=1)
            while d <= d_to:
                months.append(d); d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            for m0 in months:
                ws = self._sheet(wb, m0)
                if ws is None:
                    missing.append(m0); continue
                for hr, col in find_blocks(ws):
                    for r, d in walk(ws, hr, col, m0.year):
                        if d is None or d.weekday() >= 5 or not (d_from <= d <= d_to): continue
                        name = ws.cell(r, col + 2).value
                        if not (isinstance(name, str) and name.strip()): continue
                        if want is not None and norm(name) not in want: continue
                        if ws.cell(r, col + 4).value not in (None, ""): continue
                        st = norm(ws.cell(r, col + 5).value)
                        if st not in ("", "cl") or (d, norm(name)) in seen: continue
                        seen.add((d, norm(name)))
                        out.append({"date": d, "name": name.strip(), "status": st})
            return out, missing
        finally:
            wb.close()

    def update(self, ops, dry=False):
        """ops: {kind:'in'|'cl', date, name, reported, status}. -> [(state, detail)]"""
        try:
            wb = self._load()
        except Exception as e:
            return [("error", f"cannot open tracker: {e}")] * len(ops)
        try:
            res, wrote, row_indexes, fills, status_alignments, reported_alignments = [], False, {}, {}, {}, {}
            written_cells = []
            current_month = self._sheet(wb, datetime.now(IST).date())
            for op in ops:
                ws = self._sheet(wb, op["date"])
                if ws is None:
                    res.append(("not_found", f"no tab '{op['date']:%b - %y}' yet - copy last month's tab and rename it")); continue
                if ws.title not in row_indexes:
                    rows = {}
                    for hr, col in find_blocks(ws):
                        for r, d in walk(ws, hr, col, op["date"].year):
                            if d is not None:
                                name = ws.cell(r, col + 2).value
                                if isinstance(name, str) and name.strip():
                                    rows.setdefault((d, norm(name)), (r, col))
                    row_indexes[ws.title] = rows
                hit = row_indexes[ws.title].get((op["date"], norm(op["name"])))
                if not hit:
                    res.append(("not_found", "date/name not in sheet")); continue
                r, col = hit
                rep, st = ws.cell(r, col + 4), ws.cell(r, col + 5)
                if op["kind"] == "in":
                    if rep.value not in (None, ""):
                        res.append(("skipped", f"already {rep.value}")); continue
                    if norm(st.value) not in ("", "cl"):
                        res.append(("skipped", f"status is {st.value}")); continue
                    new = {"rep": op["reported"], "st": op["status"]}
                else:
                    if rep.value not in (None, "") or st.value not in (None, ""):
                        res.append(("skipped", "already filled")); continue
                    new = {"st": "CL"}
                if not dry:
                    format_ws = current_month if new["st"] == "CL" and current_month is not None else ws
                    fill = status_fill(format_ws, new["st"], fills)
                    status_align = status_alignment(format_ws, new["st"], status_alignments)
                    if "rep" in new:
                        rep.value = new["rep"]
                        alignment = reported_alignment(ws, col, op["date"].year, reported_alignments)
                        if alignment is not None:
                            rep.alignment = alignment
                    st.value = new["st"]
                    st.fill = fill
                    if status_align is not None:
                        st.alignment = status_align
                    written_cells.append((ws.title, r, col, new))
                wrote = True
                res.append(("would_write" if dry else "written", ""))
            if wrote and not dry:
                err = self._save(wb)
                if err:
                    return [("error", err) if state == "written" else (state, detail) for state, detail in res]
                try:
                    check = load_workbook(self.path, read_only=True)
                    try:
                        for title, row, col, expected in written_cells:
                            saved_ws = check[title]
                            if ("rep" in expected
                                    and saved_ws.cell(row, col + 4).value != expected["rep"]):
                                raise OSError(
                                    f"saved workbook read-back mismatch at {title}!{get_column_letter(col + 4)}{row}"
                                )
                            if saved_ws.cell(row, col + 5).value != expected["st"]:
                                raise OSError(
                                    f"saved workbook read-back mismatch at {title}!{get_column_letter(col + 5)}{row}"
                                )
                    finally:
                        check.close()
                except Exception as e:
                    return [("error", f"write could not be verified in the tracker: {e}")
                            if state == "written" else (state, detail) for state, detail in res]
            return res
        finally:
            wb.close()

    def _save(self, wb):
        try:
            os.makedirs(BACKUPS, exist_ok=True)
            bk = os.path.join(BACKUPS, f"{os.path.splitext(os.path.basename(self.path))[0]}_{date.today()}.xlsx")
            if not os.path.exists(bk):
                shutil.copyfile(self.path, bk)           # first write of the day -> backup
            for old in sorted(glob.glob(os.path.join(BACKUPS, "*.xlsx")))[:-14]:
                os.remove(old)
            tmp = os.path.join(APP_DIR, "_tmp.xlsx")
            baseline = getattr(wb, "_tracker_baseline", {})
            new_sheets = [ws.title for ws in wb.worksheets if ws.title not in baseline]
            if new_sheets:
                wb.save(tmp)
            return self._save_through_excel(wb, baseline, new_sheets, tmp)
        except PermissionError:
            return LOCKED + " Nothing was written."
        except Exception as e:
            return str(e)

    @staticmethod
    def _excel_color(color, interior):
        if color.type == "rgb" and color.rgb:
            rgb = color.rgb[-6:]
            interior.Color = int(rgb[4:6] + rgb[2:4] + rgb[0:2], 16)
        elif color.type == "indexed" and color.indexed is not None:
            interior.ColorIndex = color.indexed
        elif color.type == "theme" and color.theme is not None:
            interior.ThemeColor = color.theme + 1
            interior.TintAndShade = color.tint or 0
        else:
            raise ValueError(f"Excel cannot apply this workbook color ({color.type}).")

    @classmethod
    def _apply_fill(cls, cell, fill):
        interior = cell.Interior
        if fill.patternType is None:
            interior.Pattern = -4142
            return
        if fill.patternType != "solid":
            raise ValueError(f"Excel write does not support fill pattern '{fill.patternType}'.")
        interior.Pattern = 1
        cls._excel_color(fill.fgColor, interior)

    @staticmethod
    def _apply_alignment(cell, alignment):
        horizontal = {
            "general": 1, "left": -4131, "center": -4108, "right": -4152,
            "fill": 5, "justify": -4130, "centerContinuous": 7,
            "distributed": -4117,
        }
        vertical = {
            "top": -4160, "center": -4108, "bottom": -4107,
            "justify": -4130, "distributed": -4117,
        }
        if alignment.horizontal in horizontal:
            cell.HorizontalAlignment = horizontal[alignment.horizontal]
        if alignment.vertical in vertical:
            cell.VerticalAlignment = vertical[alignment.vertical]
        cell.WrapText = bool(alignment.wrap_text)
        cell.ShrinkToFit = bool(alignment.shrink_to_fit)
        cell.IndentLevel = alignment.indent or 0

    @staticmethod
    def _same_excel_value(actual, expected):
        if expected in (None, ""):
            return actual in (None, "")
        if isinstance(expected, datetime) and isinstance(actual, datetime):
            return actual == expected
        return actual == expected

    @staticmethod
    def _same_path(path_a, path_b):
        try:
            return os.path.samefile(path_a, path_b)
        except OSError:
            normalize = lambda path: os.path.normcase(os.path.realpath(os.path.abspath(path)))
            return normalize(path_a) == normalize(path_b)

    def _save_through_excel(self, wb, baseline, new_sheets, staged_path):
        try:
            import pythoncom
            from win32com.client import DispatchEx, GetObject
        except ImportError as e:
            return (
                "Excel-based tracker writing requires pywin32 and desktop Excel. "
                f"Install the updated app dependencies, then retry. ({e})"
            )

        initialized = False
        app = target = staged = None
        own_app = opened_target = False
        previous_alerts = None
        try:
            pythoncom.CoInitialize()
            initialized = True
            try:
                target = GetObject(os.path.abspath(self.path))
                if not self._same_path(target.FullName, self.path):
                    target = None
            except Exception:
                target = None
            if target is not None:
                app = target.Application
            else:
                app = DispatchEx("Excel.Application")
                own_app = True

            previous_alerts = app.DisplayAlerts
            app.DisplayAlerts = False
            if target is None:
                target = app.Workbooks.Open(
                    os.path.abspath(self.path), UpdateLinks=0, ReadOnly=False,
                    IgnoreReadOnlyRecommended=True, AddToMru=False,
                )
                opened_target = True
            if target.ReadOnly:
                return LOCKED + " Excel opened the workbook read-only; nothing was written."

            existing_names = {
                target.Worksheets.Item(i).Name.lower()
                for i in range(1, target.Worksheets.Count + 1)
            }
            for title in new_sheets:
                if title.lower() in existing_names:
                    return (
                        f"Cannot add '{title}': that tab appeared in the live workbook while "
                        "the tracker was being prepared. Refresh and retry."
                    )

            changed_cells = []
            for ws in wb.worksheets:
                if ws.title not in baseline:
                    continue
                old_cells = baseline[ws.title]
                for cell in ws._cells.values():
                    old_value, old_style = old_cells.get(cell.coordinate, (None, None))
                    style = cell._style
                    value_changed = cell.value != old_value
                    old_fill = old_style.fillId if old_style is not None else 0
                    old_alignment = old_style.alignmentId if old_style is not None else 0
                    new_fill = style.fillId if style is not None else 0
                    new_alignment = style.alignmentId if style is not None else 0
                    fill_changed = new_fill != old_fill
                    alignment_changed = new_alignment != old_alignment
                    if value_changed or fill_changed or alignment_changed:
                        changed_cells.append(
                            (ws.title, cell.coordinate, old_value, cell.value, cell,
                             value_changed, fill_changed, alignment_changed)
                        )

            destinations = []
            for (title, coordinate, old_value, new_value, source_cell,
                 value_changed, fill_changed, alignment_changed) in changed_cells:
                destination = target.Worksheets.Item(title).Range(coordinate)
                current_value = destination.Value
                if value_changed and not (
                    self._same_excel_value(current_value, old_value)
                    or self._same_excel_value(current_value, new_value)
                ):
                    return (
                        f"Tracker changed in Excel at {title}!{coordinate} while the update "
                        "was being prepared. Nothing was overwritten; retry the update."
                    )
                destinations.append((destination, current_value, new_value, source_cell,
                                     value_changed, fill_changed, alignment_changed))

            if new_sheets:
                staged = app.Workbooks.Open(
                    os.path.abspath(staged_path), UpdateLinks=0, ReadOnly=True,
                    AddToMru=False,
                )
                for title in new_sheets:
                    staged_sheet = staged.Worksheets.Item(title)
                    sheet_position = wb.sheetnames.index(title)
                    previous_sheet = wb.sheetnames[sheet_position - 1] if sheet_position else None
                    if previous_sheet and previous_sheet.lower() in existing_names:
                        staged_sheet.Copy(After=target.Worksheets.Item(previous_sheet))
                    else:
                        staged_sheet.Copy(Before=target.Worksheets.Item(1))
                    copied_sheet = app.ActiveSheet
                    existing_names.add(copied_sheet.Name.lower())

            for (destination, current_value, new_value, source_cell,
                 value_changed, fill_changed, alignment_changed) in destinations:
                if value_changed and not self._same_excel_value(current_value, new_value):
                    if isinstance(new_value, str):
                        destination.NumberFormat = "@"
                        destination.Value = new_value
                        destination.NumberFormat = source_cell.number_format
                    else:
                        destination.Value = new_value
                if fill_changed:
                    self._apply_fill(destination, source_cell.fill)
                if alignment_changed:
                    self._apply_alignment(destination, source_cell.alignment)

            target.Save()
            return None
        except Exception as e:
            return f"Excel could not save the tracker: {e}"
        finally:
            if staged is not None:
                try:
                    staged.Close(SaveChanges=False)
                except Exception:
                    pass
            if target is not None and opened_target:
                try:
                    target.Close(SaveChanges=False)
                except Exception:
                    pass
            if app is not None and previous_alerts is not None:
                try:
                    app.DisplayAlerts = previous_alerts
                except Exception:
                    pass
            if app is not None and own_app:
                try:
                    app.Quit()
                except Exception:
                    pass
            if initialized:
                pythoncom.CoUninitialize()
