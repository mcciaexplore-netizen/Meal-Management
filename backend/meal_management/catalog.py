from .auth import audit, require_actor
from .errors import DomainError
from .security import required_text


_CATALOGS = {
    "LOCATION": "locations",
    "SCANNER": "scanner_devices",
    "MEAL_TYPE": "meal_types",
}
_LIMITS = {
    "LOCATION": (32, 150),
    "SCANNER": (64, 150),
    "MEAL_TYPE": (32, 100),
}
_SHARED_COLUMNS = {
    "LOCATION": "location_id",
    "SCANNER": "scanner_id",
}


def _shared_ids(tx):
    row = tx.one(
        "SELECT p.scanner_id, p.meal_type_id, d.location_id "
        "FROM scan_app_settings p JOIN scanner_devices d ON d.id = p.scanner_id WHERE p.id = 1"
    )
    return {} if row is None else dict(row)


def _protected(error):
    return getattr(error, "errno", None) == 1644


def _positive_id(value, name):
    if type(value) is not int or not 1 <= value <= 2**64 - 1:
        raise DomainError("INVALID_" + name)
    return value


class CatalogService:
    def __init__(self, db):
        self.db = db

    def create_location(self, context, code, name):
        code = required_text(code, "LOCATION_CODE", 32)
        name = required_text(name, "LOCATION_NAME", 150)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            try:
                location_id = tx.insert(
                    "INSERT INTO locations (code, name, is_active) VALUES (%s, %s, %s)",
                    (code, name, True),
                )
            except Exception as error:
                if getattr(error, "errno", None) == 1062:
                    raise DomainError("LOCATION_CODE_EXISTS") from None
                raise
            audit(
                tx, actor.staff_id, "LOCATION_CREATED", "locations", location_id,
                after={"code": code, "name": name, "is_active": True},
            )
        return location_id

    def create_scanner(self, context, code, name, location_id):
        code = required_text(code, "SCANNER_CODE", 64)
        name = required_text(name, "SCANNER_NAME", 150)
        _positive_id(location_id, "LOCATION_ID")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            location = tx.one(
                "SELECT id, is_active FROM locations WHERE id = %s FOR SHARE",
                (location_id,),
            )
            if location is None or not location["is_active"]:
                raise DomainError("LOCATION_UNAVAILABLE")
            try:
                scanner_id = tx.insert(
                    "INSERT INTO scanner_devices (code, name, location_id, is_active) "
                    "VALUES (%s, %s, %s, %s)",
                    (code, name, location_id, True),
                )
            except Exception as error:
                if getattr(error, "errno", None) == 1062:
                    raise DomainError("SCANNER_CODE_EXISTS") from None
                raise
            audit(
                tx, actor.staff_id, "SCANNER_CREATED", "scanner_devices", scanner_id,
                after={"code": code, "name": name, "location_id": location_id, "is_active": True},
            )
        return scanner_id

    def create_meal_type(self, context, code, name):
        code = required_text(code, "MEAL_TYPE_CODE", 32)
        name = required_text(name, "MEAL_TYPE_NAME", 100)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            try:
                meal_type_id = tx.insert(
                    "INSERT INTO meal_types (code, name, is_active) VALUES (%s, %s, %s)",
                    (code, name, True),
                )
            except Exception as error:
                if getattr(error, "errno", None) == 1062:
                    raise DomainError("MEAL_TYPE_CODE_EXISTS") from None
                raise
            audit(
                tx, actor.staff_id, "MEAL_TYPE_CREATED", "meal_types", meal_type_id,
                after={"code": code, "name": name, "is_active": True},
            )
        return meal_type_id

    def _set_active(self, context, catalog, identifier, is_active):
        if catalog not in _CATALOGS:
            raise DomainError("INVALID_CATALOG")
        _positive_id(identifier, catalog + "_ID")
        if type(is_active) is not bool:
            raise DomainError("INVALID_" + catalog + "_STATUS")
        table = _CATALOGS[catalog]
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                f"SELECT id, code, name, is_active FROM {table} WHERE id = %s FOR UPDATE",
                (identifier,),
            )
            if row is None:
                raise DomainError(catalog + "_NOT_FOUND")
            previous = bool(row["is_active"])
            if previous == is_active:
                return {"id": identifier, "is_active": is_active}
            if not is_active and catalog in _SHARED_COLUMNS and _shared_ids(tx).get(_SHARED_COLUMNS[catalog]) == identifier:
                raise DomainError("SHARED_SCANNER_SETTING_PROTECTED")
            try:
                tx.execute(
                    f"UPDATE {table} SET is_active = %s WHERE id = %s",
                    (is_active, identifier),
                )
            except Exception as error:
                if _protected(error):
                    raise DomainError("SHARED_SCANNER_SETTING_PROTECTED") from None
                raise
            audit(
                tx, actor.staff_id, catalog + "_STATUS_CHANGED", table, identifier,
                before={"is_active": previous}, after={"is_active": is_active, "code": row["code"], "name": row["name"]},
            )
        return {"id": identifier, "is_active": is_active}

    def _update(self, context, catalog, identifier, code, name):
        _positive_id(identifier, catalog + "_ID")
        code_limit, name_limit = _LIMITS[catalog]
        code = required_text(code, catalog + "_CODE", code_limit)
        name = required_text(name, catalog + "_NAME", name_limit)
        table = _CATALOGS[catalog]
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one(f"SELECT id, code, name FROM {table} WHERE id = %s FOR UPDATE", (identifier,))
            if row is None:
                raise DomainError(catalog + "_NOT_FOUND")
            if (row["code"], row["name"]) == (code, name):
                return {"id": identifier, "code": code, "name": name}
            if code != row["code"] and catalog in _SHARED_COLUMNS and _shared_ids(tx).get(_SHARED_COLUMNS[catalog]) == identifier:
                raise DomainError("SHARED_SCANNER_SETTING_PROTECTED")
            try:
                tx.execute(f"UPDATE {table} SET code = %s, name = %s WHERE id = %s", (code, name, identifier))
            except Exception as error:
                if getattr(error, "errno", None) == 1062:
                    raise DomainError(catalog + "_CODE_EXISTS") from None
                if _protected(error):
                    raise DomainError("SHARED_SCANNER_SETTING_PROTECTED") from None
                raise
            audit(
                tx, actor.staff_id, catalog + "_UPDATED", table, identifier,
                before={"code": row["code"], "name": row["name"]}, after={"code": code, "name": name},
            )
        return {"id": identifier, "code": code, "name": name}

    def update_location(self, context, location_id, code, name):
        return self._update(context, "LOCATION", location_id, code, name)

    def update_scanner(self, context, scanner_id, code, name):
        return self._update(context, "SCANNER", scanner_id, code, name)

    def update_meal_type(self, context, meal_type_id, code, name):
        return self._update(context, "MEAL_TYPE", meal_type_id, code, name)

    def update_department(self, context, department_id, name):
        _positive_id(department_id, "DEPARTMENT_ID")
        name = required_text(name, "DEPARTMENT_NAME", 100)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT id, name FROM departments WHERE id = %s FOR UPDATE", (department_id,))
            if row is None:
                raise DomainError("DEPARTMENT_NOT_FOUND")
            if row["name"] == name:
                return {"id": department_id, "name": name}
            try:
                tx.execute("UPDATE departments SET name = %s WHERE id = %s", (name, department_id))
            except Exception as error:
                if getattr(error, "errno", None) == 1062:
                    raise DomainError("DEPARTMENT_NAME_EXISTS") from None
                raise
            audit(
                tx, actor.staff_id, "DEPARTMENT_UPDATED", "departments", department_id,
                before={"name": row["name"]}, after={"name": name},
            )
        return {"id": department_id, "name": name}

    def _active_employees(self, tx, department_id):
        return tx.one(
            "SELECT COUNT(*) AS total FROM employees e "
            "LEFT JOIN employee_archives a ON a.employee_id = e.id "
            "WHERE e.department_id = %s AND e.is_active = 1 AND a.employee_id IS NULL",
            (department_id,),
        )["total"]

    def department_usage(self, context, department_id):
        _positive_id(department_id, "DEPARTMENT_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            if tx.one("SELECT id FROM departments WHERE id = %s", (department_id,)) is None:
                raise DomainError("DEPARTMENT_NOT_FOUND")
            return {"id": department_id, "active_employees": int(self._active_employees(tx, department_id))}

    def set_department_active(self, context, department_id, is_active, confirm=False):
        _positive_id(department_id, "DEPARTMENT_ID")
        if type(is_active) is not bool or type(confirm) is not bool:
            raise DomainError("INVALID_DEPARTMENT_STATUS")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT id, name, is_active FROM departments WHERE id = %s FOR UPDATE", (department_id,))
            if row is None:
                raise DomainError("DEPARTMENT_NOT_FOUND")
            previous = bool(row["is_active"])
            if previous == is_active:
                return {"id": department_id, "is_active": is_active}
            active_employees = int(self._active_employees(tx, department_id))
            if not is_active and active_employees and not confirm:
                raise DomainError("DEPARTMENT_HAS_ACTIVE_EMPLOYEES")
            tx.execute("UPDATE departments SET is_active = %s WHERE id = %s", (is_active, department_id))
            audit(
                tx, actor.staff_id, "DEPARTMENT_STATUS_CHANGED", "departments", department_id,
                before={"is_active": previous},
                after={"is_active": is_active, "name": row["name"], "active_employees": active_employees},
            )
        return {"id": department_id, "is_active": is_active, "active_employees": active_employees}

    def set_location_active(self, context, location_id, is_active):
        return self._set_active(context, "LOCATION", location_id, is_active)

    def set_scanner_active(self, context, scanner_id, is_active):
        return self._set_active(context, "SCANNER", scanner_id, is_active)

    def set_meal_type_active(self, context, meal_type_id, is_active):
        return self._set_active(context, "MEAL_TYPE", meal_type_id, is_active)
