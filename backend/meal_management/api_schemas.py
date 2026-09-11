from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, field_validator, model_validator

from .errors import DomainError
from .meals import normalize_visitor_details


Identifier = Annotated[StrictInt, Field(gt=0, le=2**64 - 1)]
Quantity = Annotated[StrictInt, Field(gt=0, le=65535)]
Name = Annotated[str, Field(min_length=1, max_length=150)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def validate_dates_and_text(cls, value):
        if isinstance(value, datetime) and value.utcoffset() is None:
            raise ValueError("Timezone is required")
        if isinstance(value, str) and (not value or any(ord(character) < 32 for character in value)):
            raise ValueError("Nonempty text without control characters is required")
        return value


class LoginInput(InputModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: SecretStr

    @field_validator("password")
    @classmethod
    def password_size(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 1024:
            raise ValueError("Invalid password length")
        return value


class EmployeeInput(InputModel):
    employee_code: Annotated[str, Field(min_length=1, max_length=32)]
    full_name: Name
    email: Annotated[str, Field(min_length=3, max_length=254)]
    department_id: Identifier
    selfie_object_key: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    expires_at: datetime | None = None


class EmployeeUpdate(InputModel):
    employee_code: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    full_name: Name | None = None
    email: Annotated[str, Field(min_length=3, max_length=254)] | None = None
    department_id: Identifier | None = None
    selfie_object_key: Annotated[str, Field(min_length=1, max_length=512)] | None = None


class BulkEmployeeInput(InputModel):
    employee_code: Annotated[str, Field(min_length=1, max_length=32)]
    full_name: Name
    email: Annotated[str, Field(min_length=3, max_length=254)]
    department_id: Identifier


class BulkEmployeesInput(InputModel):
    request_id: UUID
    employees: Annotated[list[BulkEmployeeInput], Field(min_length=1, max_length=100)]


class EmailApprovalInput(InputModel):
    email_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]

    @field_validator("email_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Email identifiers must be unique")
        return value


class EmailProcessInput(EmailApprovalInput):
    email_ids: Annotated[list[Identifier], Field(min_length=1, max_length=10)]


class ExpiryInput(InputModel):
    expires_at: datetime | None = None


class RevokeInput(InputModel):
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class AuthorizationInput(InputModel):
    request_id: UUID
    master_qr_id: Identifier
    waiter_id: Identifier
    scanner_code: Annotated[str, Field(min_length=1, max_length=64)]
    meal_type_id: Identifier
    quantity: Quantity
    visitor_name: Name
    visitor_organization: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    visit_purpose: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    expires_at: datetime | None = None


class VisitorDetailsInput(InputModel):
    company_name: Name
    name: Name
    email: Annotated[str, Field(min_length=3, max_length=254)]
    phone: Annotated[str, Field(min_length=7, max_length=32)]

    @model_validator(mode="after")
    def normalized(self):
        try:
            values = normalize_visitor_details(self.model_dump())
        except DomainError:
            raise ValueError("Invalid visitor details") from None
        for key, value in values.items():
            setattr(self, key, value)
        return self


class ScanReadBody(InputModel):
    request_id: UUID
    token: SecretStr
    meal_type_id: Identifier
    scanner_code: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("token")
    @classmethod
    def token_size(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 256:
            raise ValueError("Invalid credential length")
        return value


class ScanBody(ScanReadBody):
    quantity: Quantity = 1
    authorization_id: Identifier | None = None
    visitor_details: VisitorDetailsInput | None = None

    @model_validator(mode="after")
    def visitor_scope(self):
        if self.visitor_details is not None and (self.authorization_id is not None or self.quantity != 1):
            raise ValueError("Visitor details require exactly one meal without a legacy authorization")
        return self


class ScannerReadBody(InputModel):
    request_id: UUID
    token: SecretStr

    @field_validator("token")
    @classmethod
    def token_size(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 256:
            raise ValueError("Invalid credential length")
        return value


class ScannerVisitorBody(ScannerReadBody):
    visitor_details: VisitorDetailsInput


class ScannerActivationBody(InputModel):
    activation_code: SecretStr

    @field_validator("activation_code")
    @classmethod
    def activation_code_size(cls, value):
        if not 12 <= len(value.get_secret_value()) <= 256:
            raise ValueError("Invalid activation code length")
        return value


class DepartmentInput(InputModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class CatalogInput(InputModel):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Name


class ScannerInput(InputModel):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    name: Name
    location_id: Identifier


class StaffInput(InputModel):
    display_name: Name
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: SecretStr
    roles: Annotated[list[Literal["ADMIN", "WAITER", "AUDITOR"]], Field(min_length=1, max_length=3)]


class ActiveInput(InputModel):
    is_active: StrictBool
