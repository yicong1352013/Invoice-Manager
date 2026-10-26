"""Detail navigation follows the full authorized list, including page boundaries."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.invoice import Invoice, InvoiceStatus
from app.models.user import UserRole
from app.routers.invoices import get_invoice_navigation, list_invoices


class AsyncDb:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    async def scalar(self, query):
        return self.session.scalar(query)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield AsyncDb(session)
    engine.dispose()


def user(username="alice", admin=False):
    return SimpleNamespace(username=username, role=UserRole.ADMIN if admin else UserRole.USER)


def add_invoice(db, invoice_id, age=0, owner="alice", status=InvoiceStatus.CONFIRMED, issued="2026-10-24"):
    invoice = Invoice(id=invoice_id, file_name=f"{invoice_id}.pdf", file_type="pdf", file_data=b"sample",
                      owner=owner, status=status, issue_date=date.fromisoformat(issued),
                      created_at=datetime(2026, 10, 24, 12) - timedelta(hours=age))
    db.session.add(invoice)
    db.session.flush()
    return invoice


def navigate(db, invoice_id, current_user=None, **filters):
    values = {"status": None, "owner": None, "start_date": None, "end_date": None, **filters}
    return asyncio.run(get_invoice_navigation(invoice_id, db=db, current_user=current_user or user(), **values)).model_dump()


def listing(db, page=1, page_size=20, current_user=None, **filters):
    values = {"status": None, "owner": None, "start_date": None, "end_date": None, **filters}
    return asyncio.run(list_invoices(page=page, page_size=page_size, db=db, current_user=current_user or user(), **values))


def test_single_invoice_has_no_neighbors(db):
    add_invoice(db, 101)
    assert navigate(db, 101) == {"previous_id": None, "next_id": None}


def test_navigation_uses_created_time_and_non_contiguous_ids(db):
    add_invoice(db, 10, age=0)
    add_invoice(db, 900, age=1)
    add_invoice(db, 42, age=2)
    assert navigate(db, 10) == {"previous_id": None, "next_id": 900}
    assert navigate(db, 900) == {"previous_id": 10, "next_id": 42}
    assert navigate(db, 42) == {"previous_id": 900, "next_id": None}


def test_equal_timestamp_tiebreaker_matches_list_and_crosses_page(db):
    for invoice_id in [10, 70, 30, 90, 50]:
        add_invoice(db, invoice_id)
    first_page = listing(db, page=1, page_size=2)
    second_page = listing(db, page=2, page_size=2)
    assert [item.id for item in first_page.items] == [90, 70]
    assert [item.id for item in second_page.items] == [50, 30]
    assert first_page.total == second_page.total == 5
    assert navigate(db, 70) == {"previous_id": 90, "next_id": 50}
    assert navigate(db, 50) == {"previous_id": 70, "next_id": 30}


def test_ordinary_user_neighbors_ignore_foreign_owner_parameter(db):
    add_invoice(db, 100, age=0)
    add_invoice(db, 90, age=1, owner="bob")
    add_invoice(db, 80, age=2)
    add_invoice(db, 70, age=3, owner="bob")
    add_invoice(db, 60, age=4)
    assert navigate(db, 80, owner="bob") == {"previous_id": 100, "next_id": 60}
    assert [item.id for item in listing(db, owner="bob").items] == [100, 80, 60]


@pytest.mark.parametrize("current_user,invoice_id", [(user(), 90), (user(), 999), (user("admin", True), 999)])
def test_current_invoice_missing_or_inaccessible_is_404(db, current_user, invoice_id):
    add_invoice(db, 90, owner="bob")
    with pytest.raises(HTTPException) as exc:
        navigate(db, invoice_id, current_user=current_user)
    assert exc.value.status_code == 404
    assert exc.value.detail == "发票不存在"


def test_admin_can_navigate_other_owner_and_filter_owner(db):
    add_invoice(db, 100, age=0, owner="bob")
    add_invoice(db, 90, age=1)
    add_invoice(db, 80, age=2, owner="bob")
    add_invoice(db, 70, age=3)
    add_invoice(db, 60, age=4, owner="bob")
    admin = user("admin", True)
    assert navigate(db, 80, current_user=admin) == {"previous_id": 90, "next_id": 70}
    assert navigate(db, 80, current_user=admin, owner="bob") == {"previous_id": 100, "next_id": 60}
    assert [item.id for item in listing(db, current_user=admin, owner="bob").items] == [100, 80, 60]


def test_status_filter_remains_valid_after_current_invoice_leaves_filter(db):
    add_invoice(db, 100, age=0, status=InvoiceStatus.REVIEWING)
    add_invoice(db, 90, age=1)
    current = add_invoice(db, 80, age=2, status=InvoiceStatus.REVIEWING)
    add_invoice(db, 70, age=3)
    add_invoice(db, 60, age=4, status=InvoiceStatus.REVIEWING)
    current.status = InvoiceStatus.CONFIRMED
    db.session.flush()
    assert navigate(db, 80, status=InvoiceStatus.REVIEWING) == {"previous_id": 100, "next_id": 60}
    assert [item.id for item in listing(db, status=InvoiceStatus.REVIEWING).items] == [100, 60]


def test_date_filter_is_inclusive_and_shared_with_list(db):
    add_invoice(db, 100, age=0, issued="2026-10-19")
    add_invoice(db, 90, age=1, issued="2026-10-20")
    add_invoice(db, 80, age=2, issued="2026-10-30")
    add_invoice(db, 70, age=3, issued="2026-10-25")
    add_invoice(db, 60, age=4, issued="2026-10-26")
    filters = {"start_date": "2026-10-20", "end_date": "2026-10-25"}
    assert navigate(db, 80, **filters) == {"previous_id": 90, "next_id": 70}
    assert [item.id for item in listing(db, **filters).items] == [90, 70]
    assert navigate(db, 80, start_date="invalid", end_date="invalid") == {"previous_id": 90, "next_id": 70}
    assert listing(db, start_date="invalid", end_date="invalid").total == 5


def test_combined_filters_return_only_matching_neighbors(db):
    add_invoice(db, 100, age=0, owner="bob", issued="2026-10-20")
    add_invoice(db, 90, age=1, owner="bob", status=InvoiceStatus.REVIEWING, issued="2026-10-21")
    add_invoice(db, 80, age=2, owner="alice", issued="2026-10-22")
    add_invoice(db, 70, age=3, owner="bob", issued="2026-10-23")
    add_invoice(db, 60, age=4, owner="bob", issued="2026-10-24")
    filters = {"owner": "bob", "status": InvoiceStatus.CONFIRMED, "start_date": "2026-10-20", "end_date": "2026-10-23"}
    assert navigate(db, 80, current_user=user("admin", True), **filters) == {"previous_id": 100, "next_id": 70}
    assert [item.id for item in listing(db, current_user=user("admin", True), **filters).items] == [100, 70]
