import contextlib
import io
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from meal_management.cli import main
from meal_management.errors import DomainError
from meal_management.test_data_reset import (
    KEEP_TABLES, PHONE_TABLE, PHOTO_KEY, RUN_ORDER, SNAPSHOT_ID, WIPE_TABLES, ResetRefused, TestDataReset,
    confirmation_text, grants_allow_drop, parse_phone_ids,
)

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema.sql"
REQUIRED = ["--expect-host", "db.example.test", "--expect-db", "meal_management", "--operator-email", "admin@example.test"]


class ResetRuleTests(unittest.TestCase):
    def test_every_table_is_either_wiped_kept_or_the_phone_table(self):
        tables = set(re.findall(r"^CREATE TABLE (\w+)", SCHEMA.read_text(), re.MULTILINE)) | {"schema_migrations"}
        listed = set(WIPE_TABLES) | set(KEEP_TABLES) | {PHONE_TABLE}
        self.assertEqual(listed, tables)
        self.assertFalse(set(WIPE_TABLES) & set(KEEP_TABLES))
        for table in ("master_qr_meal_types", "master_qr_allocations", "qr_credentials", "employees", "meals",
                      "waiter_shifts", "audit_events", "meal_type_corrections", "meal_voids", "scan_attempts"):
            self.assertIn(table, WIPE_TABLES)
        for table in ("staff_accounts", "staff_account_roles", "meal_types", "departments", "locations",
                      "scanner_devices", "waiter_shift_settings", "schema_migrations"):
            self.assertIn(table, KEEP_TABLES)

    def test_snapshot_ids_follow_rds_naming(self):
        for value in ("mccia-before-reset", "rds:mccia-meal-db-2026-10-26-07-00", "a1"):
            self.assertTrue(SNAPSHOT_ID.fullmatch(value), value)
        for value in ("", "1abc", "has space", "trailing-", "double--hyphen", "a;drop", "rds:"):
            self.assertFalse(SNAPSHOT_ID.fullmatch(value), value)

    def test_only_application_photo_keys_can_be_deleted(self):
        key = "a" * 64
        for value in (key + ".jpg", key + ".png", "shift-selfies/" + key + ".jpg"):
            self.assertTrue(PHOTO_KEY.fullmatch(value))
        for value in ("other/" + key + ".jpg", key + ".gif", "shift-selfies/../" + key + ".jpg", "", "x.jpg"):
            self.assertFalse(PHOTO_KEY.fullmatch(value))

    def test_keep_phones_and_confirmation_text(self):
        self.assertEqual(parse_phone_ids(" 7, 3 "), (3, 7))
        self.assertEqual(parse_phone_ids(None), ())
        for value in ("0", "a", "3,3", "3;4", "-1"):
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, "INVALID_KEEP_PHONES"):
                parse_phone_ids(value)
        self.assertEqual(confirmation_text("meal_management", "db.example.test"), "WIPE meal_management ON db.example.test")

    def test_run_order_is_stop_dry_run_real_run_up_health(self):
        self.assertEqual([step.split(" ", 1)[1].split(" (")[0] for step in RUN_ORDER], [
            "docker compose --env-file .env.docker stop admin scanner", "Dry run", "Real run",
            "docker compose --env-file .env.docker up -d",
            "Health checks: https://<admin host>/health/ready and https://<scanner host>/health/ready",
        ])


STAFF = {
    1: {"id": 1, "display_name": "Meal Scanner", "email": "scanner@meal.local", "is_active": 1, "is_scanner": 1},
    2: {"id": 2, "display_name": "admin", "email": "mcciaexplore@gmail.com", "is_active": 1, "is_scanner": 0},
}
ROLES = {1: ["WAITER"], 2: ["ADMIN"]}
RDS_DIRECT = ["GRANT USAGE ON *.* TO `admin`@`%`", "GRANT `rds_superuser_role`@`%` TO `admin`@`%`"]
RDS_FROM_ROLE = [
    "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, RELOAD, PROCESS, REFERENCES, INDEX, ALTER ON *.* "
    "TO `admin`@`%` WITH GRANT OPTION",
]


class FakeSession:
    def __init__(self, user="admin@%", roles="`rds_superuser_role`@`%`", grants=None, staff=None, staff_roles=None):
        self.user, self.roles = user, roles
        self.direct = RDS_DIRECT if grants is None else grants
        self.from_role = RDS_FROM_ROLE if grants is None else []
        self.staff = STAFF if staff is None else staff
        self.staff_roles = ROLES if staff_roles is None else staff_roles
        self.queries = []

    def one(self, sql, params=()):
        self.queries.append(sql)
        if sql.startswith("SELECT VERSION()"):
            return {"version": "8.4.6", "name": "meal_management", "user": self.user}
        if sql.startswith("SELECT CURRENT_ROLE()"):
            return {"roles": self.roles}
        if sql.startswith("SELECT id, is_active, is_scanner FROM staff_accounts WHERE email = %s"):
            return next((row for row in self.staff.values() if row["email"] == params[0]), None)
        raise AssertionError(sql)

    def all(self, sql, params=()):
        self.queries.append(sql)
        if sql.startswith("SHOW GRANTS FOR CURRENT_USER()"):
            lines = self.direct + (self.from_role if " USING " in sql else [])
            return [{"Grants for " + self.user: line} for line in lines]
        if sql.startswith("SELECT version, status FROM schema_migrations"):
            return [{"version": version, "status": "APPLIED"} for version in range(1, 17)]
        if sql.startswith("SELECT role_code FROM staff_account_roles"):
            return [{"role_code": code} for code in self.staff_roles.get(params[0], [])]
        if sql.startswith("SELECT id, label, source"):
            return []
        raise AssertionError(sql)


class PreflightTests(unittest.TestCase):
    def preflight(self, operator="mcciaexplore@gmail.com", **session):
        reset = TestDataReset(None, "db.example.test", "meal_management", operator)
        return reset._preflight(FakeSession(**session))

    def test_rds_master_with_role_privileges_and_the_production_staff_rows_passes(self):
        self.assertEqual(self.preflight()["operator_id"], 2)
        self.assertEqual(self.preflight(operator="MCCIAEXPLORE@gmail.com")["operator_id"], 2)

    def test_privileges_only_visible_through_the_active_role_are_counted(self):
        session = FakeSession()
        TestDataReset(None, "db.example.test", "meal_management", "mcciaexplore@gmail.com")._preflight(session)
        self.assertIn("SHOW GRANTS FOR CURRENT_USER() USING `rds_superuser_role`@`%`", session.queries)
        self.assertFalse(any("USER_PRIVILEGES" in query for query in session.queries))

    def test_database_account_refusals_say_which_condition_failed(self):
        cases = (
            ({"user": "meal_runtime@172.31.%"}, "RESET_DB_ACCOUNT_IS_RUNTIME", "restricted account"),
            ({"roles": "NONE"}, "RESET_DB_ROLE_NOT_ACTIVE", "`rds_superuser_role`@`%`"),
            ({"roles": "NONE", "grants": ["GRANT SELECT ON `meal_management`.* TO `ops`@`%`"], "user": "ops@%"},
             "RESET_DB_ACCOUNT_LACKS_DROP", "ops@% has no DROP privilege on meal_management"),
            ({"roles": "x; DROP TABLE y"}, "RESET_DB_ROLES_UNREADABLE", "admin@%"),
        )
        for session, code, detail in cases:
            with self.subTest(code=code), self.assertRaises(ResetRefused) as caught:
                self.preflight(**session)
            self.assertEqual(caught.exception.code, code)
            self.assertIn(detail, caught.exception.detail)

    def test_operator_refusals_say_which_condition_failed(self):
        def staff(**changes):
            return {1: STAFF[1], 2: {**STAFF[2], **changes}}

        cases = (
            ({"operator": "someone@mcciapune.in"}, "RESET_OPERATOR_NOT_FOUND", "no staff account has the email someone@mcciapune.in"),
            ({"staff": staff(is_active=0)}, "RESET_OPERATOR_INACTIVE", "staff account 2 (mcciaexplore@gmail.com) is inactive"),
            ({"staff": staff(is_scanner=1)}, "RESET_OPERATOR_IS_SCANNER", "is a scanner account"),
            ({"staff_roles": {1: ["WAITER"], 2: ["AUDITOR"]}}, "RESET_OPERATOR_NOT_ADMIN", "has roles AUDITOR, not ADMIN"),
            ({"staff_roles": {1: ["ADMIN"]}}, "RESET_OPERATOR_NOT_ADMIN", "has roles none, not ADMIN"),
        )
        for session, code, detail in cases:
            with self.subTest(code=code), self.assertRaises(ResetRefused) as caught:
                self.preflight(**session)
            self.assertEqual(caught.exception.code, code)
            self.assertIn(detail, caught.exception.detail)
            self.assertEqual(str(caught.exception), code + ": " + caught.exception.detail)

    def test_grant_lines_are_read_for_drop_on_the_database(self):
        allowed = (
            "GRANT ALL PRIVILEGES ON *.* TO `a`@`%`",
            "GRANT SELECT, DROP ON `meal_management`.* TO `a`@`%`",
            "GRANT DROP ON `meal\\_management`.* TO `a`@`%`",
            "GRANT ALL PRIVILEGES ON `meal%`.* TO `a`@`%`",
        )
        refused = (
            "GRANT SELECT, INSERT ON *.* TO `a`@`%`",
            "GRANT DROP ON `other`.* TO `a`@`%`",
            "GRANT DROP ON `meal_management`.`meals` TO `a`@`%`",
            "GRANT `drop_role`@`%` TO `a`@`%`",
            "GRANT DROP ROLE ON *.* TO `a`@`%`",
        )
        for line in allowed:
            with self.subTest(line=line):
                self.assertTrue(grants_allow_drop([line], "meal_management"))
        for line in refused:
            with self.subTest(line=line):
                self.assertFalse(grants_allow_drop([line], "meal_management"))
        self.assertFalse(grants_allow_drop(["GRANT DROP ON `meal\\_management`.* TO `a`@`%`"], "mealxmanagement"))

    def test_host_and_database_mismatch_name_both_values(self):
        settings = type("S", (), {"db_host": "rds.example.test", "db_name": "meal_management"})()
        with self.assertRaises(ResetRefused) as caught:
            TestDataReset(settings, "other.example.test", "meal_management", "a@mcciapune.in")._connect()
        self.assertEqual(str(caught.exception), "RESET_HOST_MISMATCH: DB_HOST is rds.example.test but --expect-host is other.example.test")


class ResetCommandTests(unittest.TestCase):
    def run_cli(self, *extra, tty=True, typed="WIPE meal_management ON db.example.test"):
        errors = io.StringIO()
        with patch("meal_management.cli.Settings.from_env"), \
                patch("meal_management.test_data_reset.TestDataReset") as reset, \
                patch("sys.stdin.isatty", return_value=tty), patch("builtins.input", return_value=typed), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = main(["reset-test-data", *REQUIRED, *extra])
        return code, reset, errors.getvalue()

    def test_dry_run_is_the_default(self):
        code, reset, _ = self.run_cli()
        self.assertEqual(code, 0)
        reset.return_value.dry_run.assert_called_once()
        reset.return_value.run.assert_not_called()

    def test_real_run_needs_snapshot_and_a_terminal(self):
        code, reset, errors = self.run_cli("--confirm")
        self.assertEqual((code, errors.strip()), (1, "RESET_SNAPSHOT_ID_REQUIRED"))
        reset.return_value.run.assert_not_called()
        code, reset, errors = self.run_cli("--confirm", "--snapshot-id", "snap-1", tty=False)
        self.assertEqual((code, errors.strip()), (1, "RESET_REQUIRES_INTERACTIVE_TERMINAL"))
        reset.return_value.run.assert_not_called()

    def test_real_run_passes_typed_confirmation_snapshot_and_s3_choice(self):
        code, reset, _ = self.run_cli("--confirm", "--snapshot-id", "snap-1", "--keep-phones", "4,2", "--output-dir", "/tmp/out")
        self.assertEqual(code, 0)
        args, kwargs = reset.return_value.run.call_args
        self.assertEqual(args, (True, "snap-1", "WIPE meal_management ON db.example.test", Path("/tmp/out")))
        self.assertEqual(kwargs, {"include_s3": False})
        self.assertEqual(reset.call_args.kwargs["keep_phones"], (2, 4))


    def test_refusal_reason_is_printed_after_the_code(self):
        errors = io.StringIO()
        refusal = ResetRefused("RESET_OPERATOR_NOT_ADMIN", "staff account 2 (a@b.in) has roles WAITER, not ADMIN")
        with patch("meal_management.cli.Settings.from_env"), \
                patch("meal_management.test_data_reset.TestDataReset") as reset, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            reset.return_value.dry_run.side_effect = refusal
            code = main(["reset-test-data", *REQUIRED])
        self.assertEqual((code, errors.getvalue().strip()),
                         (1, "RESET_OPERATOR_NOT_ADMIN: staff account 2 (a@b.in) has roles WAITER, not ADMIN"))


if __name__ == "__main__":
    unittest.main()
