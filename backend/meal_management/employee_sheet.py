from .errors import DomainError
from .visitor_batches import MAX_FILE_BYTES, XLSX_TYPE, _rows_from_xlsx

MAX_EMPLOYEES = 100


def read_employee_sheet(data, content_type, filename):
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DomainError("EMPLOYEE_FILE_REQUIRED")
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("EMPLOYEE_FILE_TOO_LARGE")
    name = (filename or "").strip().lower()
    kind = (content_type or "").split(";")[0].strip().lower()
    if not (name.endswith(".xlsx") and kind in {XLSX_TYPE, "application/octet-stream"} and bytes(data[:4]) == b"PK\x03\x04"):
        raise DomainError("EMPLOYEE_FILE_TYPE")
    try:
        table = _rows_from_xlsx(bytes(data), MAX_EMPLOYEES)
    except DomainError as error:
        raise DomainError(error.code.replace("VISITOR_FILE_", "EMPLOYEE_FILE_", 1)) from None
    table = [row for row in table if any(row)]
    if not table:
        raise DomainError("EMPLOYEE_FILE_EMPTY")
    header = list(table[0])
    while header and not header[-1]:
        header.pop()
    width = len(header)
    rows = [header]
    for values in table[1:]:
        if any(values[width:]):
            raise DomainError("EMPLOYEE_FILE_COLUMNS")
        rows.append((list(values) + [""] * width)[:width])
    if len(rows) > MAX_EMPLOYEES + 1:
        raise DomainError("EMPLOYEE_FILE_TOO_MANY_ROWS")
    return rows
