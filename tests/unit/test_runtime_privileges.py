import ast
import re
import unittest
from pathlib import Path

from meal_management import runtime_account


BACKEND = Path(__file__).resolve().parents[2] / "backend" / "meal_management"
OPERATOR_ONLY = frozenset({
    "backup.py", "cli.py", "development_seed.py", "migrations.py", "pg_transfer.py", "runtime_account.py", "transfer.py",
})
STATEMENT = re.compile(r"\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\s")


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{table}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is not None or right is not None:
            return (left or "{value}") + (right or "{value}")
    return None


def runtime_statements():
    for path in sorted(BACKEND.glob("*.py")):
        if path.name in OPERATOR_ONLY:
            continue
        consumed = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if id(node) in consumed:
                continue
            text = _text(node)
            if text is None or not STATEMENT.match(text):
                continue
            consumed.update(id(child) for child in ast.walk(node))
            yield path.name, node.lineno, " ".join(text.split())


def required_privileges(sql):
    upper = sql.upper()
    verb = upper.split()[0]
    needs = set()
    if verb in {"INSERT", "REPLACE"}:
        table = re.search(r"(?i)INTO\s+`?([a-z_{}]+)", sql)
        needs.add(("INSERT", table[1]))
        if "ON DUPLICATE KEY UPDATE" in upper or "ON CONFLICT" in upper:
            needs.add(("UPDATE", table[1]))
    elif verb == "UPDATE":
        needs.add(("UPDATE", re.match(r"(?i)\s*UPDATE\s+`?([a-z_{}]+)", sql)[1]))
    elif verb == "DELETE":
        needs.add(("DELETE", re.search(r"(?i)FROM\s+`?([a-z_{}]+)", sql)[1]))
    if re.search(r"(?i)\bFOR\s+UPDATE\b", sql):
        for table in re.findall(r"(?i)\b(?:FROM|JOIN)\s+`?([a-z_{}]+)", sql):
            needs.add(("LOCK", table))
    return needs


class RuntimePrivilegeTests(unittest.TestCase):
    def test_every_runtime_statement_fits_the_runtime_account_grants(self):
        granted = {
            "INSERT": set(runtime_account.INSERT_TABLES),
            "UPDATE": set(runtime_account.UPDATE_TABLES),
            "DELETE": set(runtime_account.DELETE_TABLES),
        }
        granted["LOCK"] = granted["UPDATE"] | granted["DELETE"]
        problems = []
        checked = 0
        for module, line, sql in runtime_statements():
            checked += 1
            for privilege, table in required_privileges(sql):
                if not table.startswith("{") and table not in granted[privilege]:
                    problems.append(f"{module}:{line} needs {privilege} on {table}: {sql[:90]}")
        self.assertGreater(checked, 150)
        self.assertEqual(problems, [])

    def test_dynamic_table_names_only_come_from_granted_catalog_tables(self):
        from meal_management.catalog import _CATALOGS

        self.assertTrue(set(_CATALOGS.values()) <= set(runtime_account.UPDATE_TABLES))

    def test_detector_flags_writes_and_locking_reads_outside_the_grants(self):
        self.assertEqual(required_privileges("DELETE FROM meals WHERE id = %s"), {("DELETE", "meals")})
        self.assertEqual(required_privileges("SELECT m.id FROM meals m JOIN servings s ON s.id = m.serving_id FOR UPDATE"),
                         {("LOCK", "meals"), ("LOCK", "servings")})
        self.assertEqual(required_privileges("SELECT id FROM meals WHERE id = %s FOR SHARE"), set())
        self.assertEqual(required_privileges("INSERT INTO login_rate_limits (a) VALUES (%s) ON DUPLICATE KEY UPDATE a = a"),
                         {("INSERT", "login_rate_limits"), ("UPDATE", "login_rate_limits")})

    def test_delete_is_limited_to_staff_role_replacement(self):
        self.assertEqual(tuple(runtime_account.DELETE_TABLES), ("staff_account_roles",))
        self.assertTrue({"scanner_phones", "departments", "meal_types"} <= set(runtime_account.UPDATE_TABLES))
