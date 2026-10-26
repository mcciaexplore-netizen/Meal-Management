import contextlib
import io
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from meal_management.cli import main
from meal_management.errors import DomainError
from meal_management.test_data_reset import (
    KEEP_TABLES, PHONE_TABLE, PHOTO_KEY, RUN_ORDER, SNAPSHOT_ID, WIPE_TABLES, confirmation_text, parse_phone_ids,
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


if __name__ == "__main__":
    unittest.main()
