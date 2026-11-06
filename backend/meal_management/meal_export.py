import os
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from .auth import audit, require_actor
from .database import Session, is_postgres
from .errors import DomainError
from .paging import newest_first
from .queries import MEAL_FROM, MEAL_SELECT, meal_filters
from .reports import scanner_phone_join

INDIA = timezone(timedelta(hours=5, minutes=30))
MAX_ROWS = 50_000
MAX_DAYS = 366
PAGE = 2_000
NO_DEPARTMENT = "No department"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MEAL_COLUMNS = (
    ("Meal no.", 10), ("Date", 12), ("Time (IST)", 10), ("Name", 30), ("Employee code", 16), ("Company", 28),
    ("Email", 32), ("Phone", 18), ("Category", 11), ("Department", 28), ("Event", 26), ("Meal type", 14),
    ("Corrected", 10), ("Recorded as", 14), ("Correction reason", 30), ("Waiter", 22), ("Location", 24),
)
REJECTED_COLUMNS = (("Date", 12), ("Time (IST)", 10), ("Who", 36), ("Reason", 30), ("Waiter", 22), ("Location", 24))


def report_range(start, end):
    if not isinstance(start, date) or not isinstance(end, date) or isinstance(start, datetime) or isinstance(end, datetime):
        raise DomainError("INVALID_DATE_RANGE")
    if end < start:
        raise DomainError("INVALID_DATE_RANGE")
    if (end - start).days + 1 > MAX_DAYS:
        raise DomainError("REPORT_RANGE_TOO_LONG")
    first = datetime.combine(start, time(), INDIA).astimezone(timezone.utc).replace(tzinfo=None)
    last = datetime.combine(end + timedelta(days=1), time(), INDIA).astimezone(timezone.utc).replace(tzinfo=None)
    return first, last


def report_filename(start, end):
    return f"meal-report_{start.isoformat()}_to_{end.isoformat()}.xlsx"


def local(moment):
    return moment.replace(tzinfo=timezone.utc).astimezone(INDIA) if moment.tzinfo is None else moment.astimezone(INDIA)


def text_cell(sheet, value, font=None):
    from openpyxl.cell import WriteOnlyCell

    cell = WriteOnlyCell(sheet, value="" if value is None else str(value))
    cell.data_type = "s"
    cell.number_format = "@"
    if font is not None:
        cell.font = font
    return cell


def value_cell(sheet, value, number_format=None, font=None):
    from openpyxl.cell import WriteOnlyCell

    if value is None or isinstance(value, str):
        return text_cell(sheet, value, font)
    cell = WriteOnlyCell(sheet, value=value)
    if number_format:
        cell.number_format = number_format
    if font is not None:
        cell.font = font
    return cell


class ExportLock:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def hold(self):
        connection = self.database._connect()
        session = Session(connection, self.database.dialect)
        postgres = is_postgres(session)
        name = "meal_report_export:" + str(getattr(self.database.settings, "db_name", ""))[:40]
        try:
            if postgres:
                got = session.one("SELECT pg_try_advisory_lock(hashtext(%s)) AS got", (name,))["got"]
            else:
                got = session.one("SELECT GET_LOCK(%s, 0) AS got", (name,))["got"]
            if got not in (1, True):
                raise DomainError("REPORT_EXPORT_RUNNING")
            try:
                yield
            finally:
                if postgres:
                    session.one("SELECT pg_advisory_unlock(hashtext(%s)) AS released", (name,))
                else:
                    session.one("SELECT RELEASE_LOCK(%s) AS released", (name,))
        finally:
            try:
                connection.close()
            except Exception:
                pass


class MealReportExport:
    def __init__(self, database, max_rows=MAX_ROWS, page=PAGE):
        self.database = database
        self.max_rows = max_rows
        self.page = page
        self.lock = ExportLock(database)

    def export(self, context, start, end, employee_id=None, department_id=None, meal_type_id=None):
        start_utc, end_utc = report_range(start, end)
        filename = report_filename(start, end)
        with self.lock.hold():
            with self.database.transaction() as tx:
                actor = require_actor(tx, context, {"ADMIN"})
                staff = tx.one("SELECT display_name, email FROM staff_accounts WHERE id = %s", (actor.staff_id,))
                filters, params = meal_filters(start_utc, end_utc, employee_id, meal_type_id, department_id)
                counted = tx.one("SELECT COUNT(*) AS n, MAX(m.id) AS top " + MEAL_FROM + " AND ".join(filters), params)
                rejected = tx.one(
                    "SELECT COUNT(*) AS n, MAX(a.id) AS top FROM scan_attempts a "
                    "WHERE a.outcome = 'REJECTED' AND a.received_at >= %s AND a.received_at < %s",
                    (start_utc, end_utc),
                )
                names = self._filter_names(tx, department_id, meal_type_id)
            if counted["n"] > self.max_rows:
                raise DomainError("REPORT_TOO_LARGE")
            if counted["n"] == 0:
                raise DomainError("REPORT_EMPTY")
            if rejected["n"] > self.max_rows:
                raise DomainError("REPORT_TOO_LARGE")
            handle, path = tempfile.mkstemp(prefix="meal-report-", suffix=".xlsx")
            os.close(handle)
            try:
                summary = self._write(path, start_utc, end_utc, filters, params, counted["top"], rejected["top"] or 0, {
                    "start": start, "end": end, "employee_id": employee_id, "names": names, "staff": staff,
                    "rejected": rejected["n"],
                })
                if summary["meals"] != counted["n"]:
                    raise DomainError("REPORT_UNCONFIRMED")
                with self.database.transaction() as tx:
                    actor = require_actor(tx, context, {"ADMIN"})
                    audit(tx, actor.staff_id, "MEAL_REPORT_EXPORTED", "meal_report", filename, after={
                        "from": start.isoformat(), "through": end.isoformat(), "employee_id": employee_id,
                        "department_id": department_id, "meal_type_id": meal_type_id,
                        "meal_rows": summary["meals"], "rejected_rows": summary["rejected"], "file": filename,
                    })
            except BaseException:
                os.unlink(path)
                raise
        return {"path": path, "filename": filename, "meal_rows": summary["meals"], "rejected_rows": summary["rejected"]}

    def _filter_names(self, tx, department_id, meal_type_id):
        names = {}
        if department_id is not None:
            row = tx.one("SELECT name FROM departments WHERE id = %s", (department_id,))
            names["department"] = row["name"] if row else f"#{department_id}"
        if meal_type_id is not None:
            row = tx.one("SELECT name FROM meal_types WHERE id = %s", (meal_type_id,))
            names["meal_type"] = row["name"] if row else f"#{meal_type_id}"
        return names

    def _pages(self, select, filters, params, time_column, id_column, top_column, top):
        cursor = None
        while True:
            page_filters = list(filters) + [top_column + " <= %s"]
            page_params = list(params) + [top]
            condition, values, order = newest_first(time_column, id_column, cursor)
            if condition is not None:
                page_filters.append(condition)
                page_params.extend(values)
            page_params.append(self.page)
            with self.database.transaction() as tx:
                rows = tx.all(select + " AND ".join(page_filters) + order, page_params)
            yield from rows
            if len(rows) < self.page:
                return
            cursor = (rows[-1][time_column.split(".")[1]], rows[-1]["id"])

    def _write(self, path, start_utc, end_utc, filters, params, top_meal, top_scan, info):
        from openpyxl import Workbook
        from openpyxl.styles import Font

        bold = Font(bold=True)
        workbook = Workbook(write_only=True)
        meals = workbook.create_sheet("Meals")
        self._header(meals, MEAL_COLUMNS, bold)
        by_kind, by_type, by_day, by_department, by_waiter = Counter(), Counter(), Counter(), Counter(), Counter()
        by_employee = defaultdict(Counter)
        employee_names = {}
        total = 0
        for row in self._pages(MEAL_SELECT, filters, params, "m.served_at", "m.id", "m.id", top_meal):
            total += 1
            moment = local(row["served_at"])
            visitor = row["kind"] == "MASTER"
            name = row["visitor_name"] if visitor else row["employee_name"]
            department = row["department_name"] or NO_DEPARTMENT
            corrected = bool(row["correction_count"])
            meals.append([
                value_cell(meals, int(row["meal_id"])),
                value_cell(meals, moment.date(), "dd mmm yyyy"),
                text_cell(meals, moment.strftime("%H:%M")),
                text_cell(meals, name),
                text_cell(meals, None if visitor else row["employee_code"]),
                text_cell(meals, row["visitor_company_name"] if visitor else row["employee_company_name"]),
                text_cell(meals, row["visitor_email"] if visitor else row["employee_email"]),
                text_cell(meals, row["visitor_phone"] if visitor else row["employee_phone"]),
                text_cell(meals, "Visitor" if visitor else "Employee"),
                text_cell(meals, department),
                text_cell(meals, row["event_name"] if visitor else None),
                text_cell(meals, row["meal_type_name"]),
                text_cell(meals, "Yes" if corrected else None),
                text_cell(meals, row["recorded_meal_type_name"] if corrected else None),
                text_cell(meals, row["correction_reason"] if corrected else None),
                text_cell(meals, row["waiter_name"]),
                text_cell(meals, row["location_name"]),
            ])
            by_kind["Visitor" if visitor else "Employee"] += 1
            by_type[row["meal_type_name"]] += 1
            by_day[moment.date()] += 1
            by_department[department] += 1
            by_waiter[row["waiter_name"] or "—"] += 1
            if not visitor:
                by_employee[row["employee_id"]][row["meal_type_name"]] += 1
                employee_names[row["employee_id"]] = (row["employee_name"], row["employee_code"])
        meals.auto_filter.ref = f"A1:{self._column(len(MEAL_COLUMNS))}{total + 1}"

        summary = workbook.create_sheet("Summary")
        for width, letter in ((34, "A"), (16, "B"), (16, "C"), (14, "D"), (14, "E"), (14, "F"), (14, "G"), (14, "H")):
            summary.column_dimensions[letter].width = width
        names = info["names"]
        staff = info["staff"] or {}
        summary.append([text_cell(summary, "Meal report", bold)])
        for label, value in (
            ("From", info["start"].strftime("%d %b %Y")), ("Through", info["end"].strftime("%d %b %Y")),
            ("Employee ID", info["employee_id"] or "All"), ("Department", names.get("department", "All")),
            ("Meal type", names.get("meal_type", "All")),
            ("Generated at (IST)", datetime.now(INDIA).strftime("%d %b %Y %H:%M")),
            ("Generated by", f"{staff.get('display_name', '')} <{staff.get('email', '')}>"),
            ("Dates are", "India time (IST) calendar days"),
        ):
            summary.append([text_cell(summary, label, bold), value_cell(summary, value)])
        summary.append([])
        for label, value in (("Total meals", total), ("Employee meals", by_kind["Employee"]), ("Visitor meals", by_kind["Visitor"]),
                             ("Rejected scans (dates only, see that sheet)", info["rejected"])):
            summary.append([text_cell(summary, label, bold), value_cell(summary, value)])
        self._table(summary, "By meal type", "Meal type", sorted(by_type.items()), bold)
        self._table(summary, "By day", "Date", sorted(by_day.items()), bold, "dd mmm yyyy")
        self._table(summary, "By department", "Department", sorted(by_department.items()), bold)
        self._table(summary, "By waiter", "Waiter", sorted(by_waiter.items()), bold)
        types = sorted({meal_type for counts in by_employee.values() for meal_type in counts})
        summary.append([])
        summary.append([text_cell(summary, "Meals by employee", bold)])
        summary.append([text_cell(summary, heading, bold) for heading in ("Name", "Employee code", *types, "Total")])
        for employee_id in sorted(employee_names, key=lambda key: (str(employee_names[key][0] or "").lower(), key)):
            counts = by_employee[employee_id]
            name, code = employee_names[employee_id]
            summary.append([text_cell(summary, name), text_cell(summary, code), *[value_cell(summary, counts[meal_type]) for meal_type in types],
                            value_cell(summary, sum(counts.values()))])

        rejected = workbook.create_sheet("Rejected scans")
        self._header(rejected, REJECTED_COLUMNS, bold)
        scan_select = (
            "SELECT a.id, a.received_at, a.rejection_code, COALESCE(ph.label, st.display_name) AS waiter_name, l.name AS location_name, "
            "c.kind AS qr_kind, e.full_name AS employee_name, e.employee_code, ma.company_name, ma.contact_name "
            "FROM scan_attempts a LEFT JOIN staff_accounts st ON st.id = a.staff_id "
            + scanner_phone_join("a.request_id") +
            "LEFT JOIN locations l ON l.id = a.location_id "
            "LEFT JOIN qr_credentials c ON c.id = a.qr_id "
            "LEFT JOIN employees e ON e.id = c.employee_id "
            "LEFT JOIN master_qr_allocations ma ON ma.qr_id = c.id WHERE "
        )
        scan_count = 0
        if top_scan:
            for row in self._pages(scan_select, ["a.outcome = 'REJECTED'", "a.received_at >= %s", "a.received_at < %s"],
                                   [start_utc, end_utc], "a.received_at", "a.id", "a.id", top_scan):
                scan_count += 1
                moment = local(row["received_at"])
                if row["employee_name"]:
                    who = f"{row['employee_name']} ({row['employee_code']})"
                elif row["company_name"] or row["contact_name"]:
                    who = " – ".join(value for value in (row["company_name"], row["contact_name"]) if value)
                else:
                    who = "Unknown QR"
                rejected.append([
                    value_cell(rejected, moment.date(), "dd mmm yyyy"), text_cell(rejected, moment.strftime("%H:%M")),
                    text_cell(rejected, who), text_cell(rejected, row["rejection_code"] or "Rejected"),
                    text_cell(rejected, row["waiter_name"]), text_cell(rejected, row["location_name"]),
                ])
        if not scan_count:
            rejected.append([text_cell(rejected, "No rejected scans for these dates.")])
        else:
            rejected.auto_filter.ref = f"A1:{self._column(len(REJECTED_COLUMNS))}{scan_count + 1}"
        workbook.save(path)
        return {"meals": total, "rejected": scan_count}

    def _header(self, sheet, columns, bold):
        for index, (_, width) in enumerate(columns, start=1):
            sheet.column_dimensions[self._column(index)].width = width
        sheet.freeze_panes = "A2"
        sheet.append([text_cell(sheet, heading, bold) for heading, _ in columns])

    def _table(self, sheet, title, label, rows, bold, number_format=None):
        sheet.append([])
        sheet.append([text_cell(sheet, title, bold)])
        sheet.append([text_cell(sheet, label, bold), text_cell(sheet, "Meals", bold)])
        for key, count in rows:
            sheet.append([value_cell(sheet, key, number_format), value_cell(sheet, count)])

    @staticmethod
    def _column(index):
        from openpyxl.utils import get_column_letter

        return get_column_letter(index)
