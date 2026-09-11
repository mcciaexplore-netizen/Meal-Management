from datetime import datetime, timezone

from .auth import require_actor
from .database import Database
from .errors import DomainError
from .models import ServerContext


def _date_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise DomainError("INVALID_DATE_RANGE")
    if start.utcoffset() is None or end.utcoffset() is None:
        raise DomainError("UTC_OFFSET_REQUIRED")
    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
    if start_utc >= end_utc:
        raise DomainError("INVALID_DATE_RANGE")
    return start_utc, end_utc


class ReportService:
    def __init__(self, database: Database):
        self.database = database

    def employee_history(
        self,
        context: ServerContext,
        employee_id: int,
        start: datetime,
        end: datetime,
        limit: int = 100,
        after_id: int = 0,
    ) -> list[dict]:
        start_utc, end_utc = _date_range(start, end)
        if type(employee_id) is not int or employee_id <= 0:
            raise DomainError("INVALID_EMPLOYEE_ID")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DomainError("INVALID_REPORT_LIMIT")
        if type(after_id) is not int or after_id < 0:
            raise DomainError("INVALID_REPORT_CURSOR")
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN", "AUDITOR"})
            records = tx.all(
                "SELECT m.id AS meal_id, m.served_at, s.id AS serving_id, "
                "s.employee_id, e.employee_code, e.full_name AS employee_name, "
                "s.meal_type_id, t.code AS meal_type_code, t.name AS meal_type_name, "
                "s.waiter_id, w.display_name AS waiter_name, "
                "s.location_id, l.code AS location_code, l.name AS location_name, "
                "s.scanner_id, d.code AS scanner_code "
                "FROM meals m "
                "JOIN servings s ON s.id = m.serving_id "
                "JOIN employees e ON e.id = s.employee_id "
                "JOIN meal_types t ON t.id = s.meal_type_id "
                "JOIN staff_accounts w ON w.id = s.waiter_id "
                "JOIN locations l ON l.id = s.location_id "
                "JOIN scanner_devices d ON d.id = s.scanner_id "
                "WHERE s.employee_id = %s AND s.kind = 'EMPLOYEE' "
                "AND m.served_at >= %s AND m.served_at < %s AND m.id > %s "
                "AND NOT EXISTS (SELECT 1 FROM meal_voids v WHERE v.meal_id = m.id) "
                "ORDER BY m.id ASC LIMIT %s",
                (employee_id, start_utc, end_utc, after_id, limit),
            )
        for record in records:
            served_at = record["served_at"]
            record["served_at"] = (
                served_at.replace(tzinfo=timezone.utc)
                if served_at.utcoffset() is None
                else served_at.astimezone(timezone.utc)
            )
        return records

    def meal_report(
        self,
        context: ServerContext,
        start: datetime,
        end: datetime,
    ) -> dict:
        start_utc, end_utc = _date_range(start, end)
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN", "AUDITOR"})
            records = tx.all(
                "WITH filtered_meals AS ("
                "SELECT m.id, m.serving_id, s.kind, "
                "s.meal_type_id, t.code AS meal_type_code, t.name AS meal_type_name, "
                "s.location_id, l.code AS location_code, l.name AS location_name, "
                "s.waiter_id, w.display_name AS waiter_name, "
                "a.authorized_by, admin.display_name AS admin_name "
                "FROM meals m "
                "JOIN servings s ON s.id = m.serving_id "
                "JOIN meal_types t ON t.id = s.meal_type_id "
                "JOIN locations l ON l.id = s.location_id "
                "JOIN staff_accounts w ON w.id = s.waiter_id "
                "LEFT JOIN visitor_authorizations a ON a.id = s.authorization_id "
                "LEFT JOIN staff_accounts admin ON admin.id = a.authorized_by "
                "WHERE m.served_at >= %s AND m.served_at < %s "
                "AND NOT EXISTS (SELECT 1 FROM meal_voids v WHERE v.meal_id = m.id)"
                ") "
                "SELECT 'TOTAL' AS section, NULL AS group_id, "
                "NULL AS code, NULL AS name, COUNT(*) AS meal_count, "
                "COUNT(DISTINCT serving_id) AS serving_count FROM filtered_meals "
                "UNION ALL "
                "SELECT 'KIND', NULL, kind, kind, COUNT(*), "
                "COUNT(DISTINCT serving_id) FROM filtered_meals GROUP BY kind "
                "UNION ALL "
                "SELECT 'MEAL_TYPE', meal_type_id, meal_type_code, meal_type_name, "
                "COUNT(*), COUNT(DISTINCT serving_id) FROM filtered_meals "
                "GROUP BY meal_type_id, meal_type_code, meal_type_name "
                "UNION ALL "
                "SELECT 'LOCATION', location_id, location_code, location_name, "
                "COUNT(*), COUNT(DISTINCT serving_id) FROM filtered_meals "
                "GROUP BY location_id, location_code, location_name "
                "UNION ALL "
                "SELECT 'WAITER', waiter_id, NULL, waiter_name, COUNT(*), "
                "COUNT(DISTINCT serving_id) FROM filtered_meals "
                "GROUP BY waiter_id, waiter_name "
                "UNION ALL "
                "SELECT 'AUTHORIZING_ADMIN', authorized_by, NULL, admin_name, "
                "COUNT(*), COUNT(DISTINCT serving_id) FROM filtered_meals "
                "WHERE kind = 'MASTER' AND authorized_by IS NOT NULL GROUP BY authorized_by, admin_name "
                "ORDER BY section, group_id, code",
                (start_utc, end_utc),
            )
        result = {
            "start": start_utc.replace(tzinfo=timezone.utc),
            "end": end_utc.replace(tzinfo=timezone.utc),
            "totals": {
                "meal_count": 0,
                "serving_count": 0,
                "employee_meals": 0,
                "visitor_meals": 0,
            },
            "by_kind": [],
            "by_meal_type": [],
            "by_location": [],
            "by_waiter": [],
            "by_authorizing_admin": [],
        }
        groups = {
            "MEAL_TYPE": ("by_meal_type", "meal_type_id"),
            "LOCATION": ("by_location", "location_id"),
            "WAITER": ("by_waiter", "waiter_id"),
            "AUTHORIZING_ADMIN": ("by_authorizing_admin", "admin_id"),
        }
        for record in records:
            counts = {
                "meal_count": int(record["meal_count"]),
                "serving_count": int(record["serving_count"]),
            }
            section = record["section"]
            if section == "TOTAL":
                result["totals"].update(counts)
            elif section == "KIND":
                kind = record["code"]
                result["by_kind"].append({"kind": kind, **counts})
                total_key = "employee_meals" if kind == "EMPLOYEE" else "visitor_meals"
                result["totals"][total_key] = counts["meal_count"]
            else:
                target, id_field = groups[section]
                result[target].append(
                    {
                        id_field: record["group_id"],
                        "code": record["code"],
                        "name": record["name"],
                        **counts,
                    }
                )
        return result
