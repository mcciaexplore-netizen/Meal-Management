from .auth import audit, require_actor
from .errors import DomainError
from .security import required_text


_CATALOGS = {
    "LOCATION": "locations",
    "SCANNER": "scanner_devices",
    "MEAL_TYPE": "meal_types",
}


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
                f"SELECT id, is_active FROM {table} WHERE id = %s FOR UPDATE",
                (identifier,),
            )
            if row is None:
                raise DomainError(catalog + "_NOT_FOUND")
            previous = bool(row["is_active"])
            if previous != is_active:
                tx.execute(
                    f"UPDATE {table} SET is_active = %s WHERE id = %s",
                    (is_active, identifier),
                )
                audit(
                    tx, actor.staff_id, catalog + "_STATUS_CHANGED", table, identifier,
                    before={"is_active": previous}, after={"is_active": is_active},
                )

    def set_location_active(self, context, location_id, is_active):
        self._set_active(context, "LOCATION", location_id, is_active)

    def set_scanner_active(self, context, scanner_id, is_active):
        self._set_active(context, "SCANNER", scanner_id, is_active)

    def set_meal_type_active(self, context, meal_type_id, is_active):
        self._set_active(context, "MEAL_TYPE", meal_type_id, is_active)
