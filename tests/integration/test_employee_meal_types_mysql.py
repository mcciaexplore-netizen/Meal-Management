from uuid import uuid4

from test_mysql_services import MealServicesFixture


class EmployeeMealTypesMySQLTests(MealServicesFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cursor = cls.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute("SELECT id, code FROM meal_types")
            cls.types = {row["code"]: row["id"] for row in cursor.fetchall()}
        finally:
            cursor.close()
        cls.target = sorted([cls.types["BREAKFAST"], cls.types["HIGH-TEA"]])

    def rows(self, statement, parameters=()):
        cursor = self.admin_connection.cursor(dictionary=True)
        try:
            cursor.execute(statement, parameters)
            return cursor.fetchall()
        finally:
            cursor.close()

    def people(self, *meal_codes):
        rows = []
        for codes in meal_codes:
            tag = uuid4().hex[:10]
            rows.append({"employee_code": f"MT-{tag}", "full_name": f"Meal Types {tag}", "email": f"mt-{tag}@mcciapune.in",
                         "company_name": "MCCIA", "phone": "+91 98765 43210",
                         "meal_type_ids": sorted(self.types[code] for code in codes)})
        result = self.employees.register_bulk(self.admin_context, uuid4(), rows)
        return [row["employee_id"] for row in result["employees"]]

    def grants(self, ids):
        placeholders = ", ".join(["%s"] * len(ids))
        found = {identifier: [] for identifier in ids}
        for row in self.rows(f"SELECT employee_id, meal_type_id FROM employee_meal_types WHERE employee_id IN ({placeholders}) "
                             "ORDER BY employee_id, meal_type_id", tuple(ids)):
            found[row["employee_id"]].append(row["meal_type_id"])
        return found

    def audits(self, ids):
        placeholders = ", ".join(["%s"] * len(ids))
        return self.rows(f"SELECT entity_id, before_data, after_data FROM audit_events WHERE action = 'EMPLOYEE_MEAL_TYPES_CHANGED' "
                         f"AND entity_id IN ({placeholders}) ORDER BY id", tuple(str(identifier) for identifier in ids))

    def test_preview_counts_and_lists_without_changing_anything(self):
        ids = self.people(*[("BREAKFAST", "LUNCH", "HIGH-TEA", "DINNER")] * 11, ("BREAKFAST", "HIGH-TEA"), ("LUNCH",))
        before = self.grants(ids)
        preview = self.employees.preview_meal_types_bulk(self.admin_context, ids, self.target)
        self.assertEqual((preview["selected"], preview["changing"], preview["unchanged"]), (13, 12, 1))
        self.assertEqual(len(preview["examples"]), 10)
        self.assertEqual(preview["examples"][0]["employee_id"], ids[0])
        self.assertEqual(preview["examples"][0]["current_meal_types"], ["Breakfast", "Lunch", "High Tea", "Dinner"])
        self.assertNotIn(ids[11], [row["employee_id"] for row in preview["examples"]])
        self.assertEqual(self.grants(ids), before)
        self.assertEqual(self.audits(ids), [])

    def test_apply_sets_exact_types_audits_changes_only_and_is_repeatable(self):
        ids = self.people(("BREAKFAST", "LUNCH", "HIGH-TEA", "DINNER"), ("BREAKFAST", "HIGH-TEA"), ("LUNCH",))
        self.employees.set_active(self.admin_context, ids[2], False)
        result = self.employees.set_meal_types_bulk(self.admin_context, ids, self.target, 2)
        self.assertEqual((result["selected"], result["changed"]), (3, 2))
        self.assertEqual(self.grants(ids), {identifier: self.target for identifier in ids})
        audits = self.audits(ids)
        self.assertEqual([row["entity_id"] for row in audits], [str(ids[0]), str(ids[2])])
        self.assertIn(str(self.types["LUNCH"]), audits[1]["before_data"])
        again = self.employees.set_meal_types_bulk(self.admin_context, ids, self.target, 0)
        self.assertEqual(again["changed"], 0)
        self.assertEqual(len(self.audits(ids)), 2)

    def test_stale_previews_removed_employees_and_inactive_types_change_nothing(self):
        from meal_management.errors import DomainError

        ids = self.people(("LUNCH",), ("DINNER",))
        before = self.grants(ids)
        with self.assertRaisesRegex(DomainError, "EMPLOYEE_MEAL_TYPES_COUNT_CHANGED"):
            self.employees.set_meal_types_bulk(self.admin_context, ids, self.target, 1)
        self.assertEqual(self.grants(ids), before)
        removed = self.people(("LUNCH",))[0]
        self.employees.archive_bulk(self.admin_context, [removed], "Test removal")
        for call in (lambda: self.employees.preview_meal_types_bulk(self.admin_context, ids + [removed], self.target),
                     lambda: self.employees.set_meal_types_bulk(self.admin_context, ids + [removed], self.target, 2)):
            with self.assertRaisesRegex(DomainError, "EMPLOYEE_NOT_FOUND"):
                call()
        inactive = self.rows("SELECT id FROM meal_types WHERE code = 'SCANNER-MEAL'")[0]["id"]
        with self.assertRaisesRegex(DomainError, "MEAL_TYPE_UNAVAILABLE"):
            self.employees.set_meal_types_bulk(self.admin_context, ids, [inactive], 2)
        self.assertEqual(self.grants(ids), before)
        self.assertEqual(self.audits(ids), [])

    def test_reactivating_an_employee_keeps_their_meal_types(self):
        employee = self.people(("BREAKFAST", "HIGH-TEA"))[0]
        self.employees.set_active(self.admin_context, employee, False)
        self.employees.set_active(self.admin_context, employee, True)
        self.assertEqual(self.grants([employee]), {employee: self.target})

    def test_bulk_import_requires_meal_types_on_every_row(self):
        from meal_management.errors import DomainError

        row = {"employee_code": "MT-" + uuid4().hex[:10], "full_name": "No Meals", "email": f"no-{uuid4().hex[:8]}@mcciapune.in",
               "company_name": "MCCIA", "phone": "+91 98765 43210"}
        count = self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"]
        for employee, code in ((row, "INVALID_EMPLOYEE_BATCH"), ({**row, "meal_type_ids": None}, "INVALID_MEAL_TYPE_IDS"),
                               ({**row, "meal_type_ids": []}, "INVALID_MEAL_TYPE_IDS")):
            with self.subTest(code=code), self.assertRaisesRegex(DomainError, code):
                self.employees.register_bulk(self.admin_context, uuid4(), [employee])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM employees")[0]["n"], count)
