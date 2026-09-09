from dataclasses import dataclass

from .accounts import StaffService
from .catalog import CatalogService
from .config import Settings
from .database import Database
from .employees import EmployeeService
from .meals import ApprovalService, MealService
from .qr import QrService
from .reports import ReportService
from .security import QrRenderer, TokenVault


@dataclass(frozen=True)
class Services:
    database: Database
    staff: StaffService
    employees: EmployeeService
    qr: QrService
    meals: MealService
    approvals: ApprovalService
    reports: ReportService
    catalog: CatalogService

    @property
    def email_queue(self):
        return self.qr.email_queue


def create_services(settings=None):
    settings = Settings.from_env() if settings is None else settings
    database = Database(settings)
    qr = QrService(database, TokenVault(settings.qr_encryption_keys), QrRenderer())
    return Services(
        database=database,
        staff=StaffService(database),
        employees=EmployeeService(database, qr),
        qr=qr,
        meals=MealService(database),
        approvals=ApprovalService(database),
        reports=ReportService(database),
        catalog=CatalogService(database),
    )
