import importlib
import inspect

from mysql_support import IsolatedMySQLTestCase


MODULES = (
    "test_email_delivery", "test_email_policy", "test_independent_apps_mysql", "test_meal_types_mysql", "test_mysql_services",
    "test_scan_app_mysql", "test_scan_receipts_mysql", "test_scanner_phones_mysql", "test_settings_mysql",
    "test_shared_scanner_mysql", "test_waiter_shifts_mysql", "test_visitor_meal_types_mysql",
)


def _restricted_copies():
    for module_name in MODULES:
        module = importlib.import_module(module_name)
        for name, candidate in inspect.getmembers(module, inspect.isclass):
            if (
                candidate.__module__ == module_name
                and issubclass(candidate, IsolatedMySQLTestCase)
                and any(attribute.startswith("test_") for attribute in dir(candidate))
            ):
                yield "RuntimeAccount" + name, type("RuntimeAccount" + name, (candidate,), {
                    "runtime_privileges": True, "__module__": __name__,
                })


globals().update(dict(_restricted_copies()))
