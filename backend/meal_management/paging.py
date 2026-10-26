import re
from datetime import datetime, timedelta

from .errors import DomainError


CURSOR_PATTERN = r"^[0-9]{1,17}\.[1-9][0-9]{0,19}$"
EPOCH = datetime(1970, 1, 1)
MICROSECOND = timedelta(microseconds=1)


def parse_cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(CURSOR_PATTERN, value):
        raise DomainError("INVALID_REPORT_CURSOR")
    micros, identifier = value.split(".")
    identifier = int(identifier)
    if identifier > 2**64 - 1:
        raise DomainError("INVALID_REPORT_CURSOR")
    try:
        moment = EPOCH + int(micros) * MICROSECOND
    except OverflowError:
        raise DomainError("INVALID_REPORT_CURSOR") from None
    return moment, identifier


def format_cursor(moment, identifier):
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None) - moment.utcoffset()
    return str((moment - EPOCH) // MICROSECOND) + "." + str(int(identifier))


def newest_first(time_column, id_column, cursor):
    order = " ORDER BY " + time_column + " DESC, " + id_column + " DESC LIMIT %s"
    if cursor is None:
        return None, (), order
    moment, identifier = cursor
    return (
        "(" + time_column + " < %s OR (" + time_column + " = %s AND " + id_column + " < %s))",
        (moment, moment, identifier),
        order,
    )


def next_cursor(rows, limit, time_key, id_key="id"):
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return format_cursor(last[time_key], last[id_key])
