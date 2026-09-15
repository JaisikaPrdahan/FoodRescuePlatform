"""
Regression test for the NGO registration response missing `ngo_id`.

Person 6's integration suite expects POST /auth/register to return the
role-specific id (donor_id/ngo_id/driver_id) of the profile row it just
created, not just the user_id — callers shouldn't have to call GET /auth/me
to learn the id of the thing they just registered.

Uses app.core.database's own engine/session (the one app.main actually reads
and writes through via get_db) for setup and assertions, rather than
tests/fixtures/seed.py's separate engine — that fixture points at a
differently-named database (see TEST_DATABASE_URL there), which isn't
guaranteed to be the same database the running app is connected to.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import AsyncSessionLocal, Base, engine
from app.main import app
from app.models import NGO, Donor, Vehicle


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_register_ngo_returns_ngo_id(db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/api/v1/auth/register", json={
            "name": "Test NGO",
            "email": "ngo_reg_test@example.com",
            "phone": "1234567890",
            "password": "TestPass123!",
            "role": "NGO",
            "organisation_name": "Reg Test NGO",
            "address": "1 Test Ave",
            "storage_capacity_kg": 500,
            "accepted_categories": ["COOKED_MEALS"],
        })

    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data.get("ngo_id") is not None, f"register response missing ngo_id: {data}"
    assert data["donor_id"] is None
    assert data["driver_id"] is None

    ngo = (await db.execute(select(NGO).where(NGO.id == data["ngo_id"]))).scalar_one_or_none()
    assert ngo is not None, "ngo_id in register response does not match any persisted NGO row"
    assert ngo.user_id == data["user_id"]


@pytest.mark.asyncio
async def test_register_donor_returns_donor_id(db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/api/v1/auth/register", json={
            "name": "Test Donor",
            "email": "donor_reg_test@example.com",
            "phone": "1234567890",
            "password": "TestPass123!",
            "role": "DONOR",
            "organisation_name": "Reg Test Donor",
            "address": "2 Test Ave",
        })

    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data.get("donor_id") is not None, f"register response missing donor_id: {data}"
    assert data["ngo_id"] is None
    assert data["driver_id"] is None

    donor = (await db.execute(select(Donor).where(Donor.id == data["donor_id"]))).scalar_one_or_none()
    assert donor is not None, "donor_id in register response does not match any persisted Donor row"
    assert donor.user_id == data["user_id"]


@pytest.mark.asyncio
async def test_register_driver_returns_driver_id(db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/api/v1/auth/register", json={
            "name": "Test Driver",
            "email": "driver_reg_test@example.com",
            "phone": "1234567890",
            "password": "TestPass123!",
            "role": "DRIVER",
            "vehicle_capacity_kg": 100,
        })

    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data.get("driver_id") is not None, f"register response missing driver_id: {data}"
    assert data["donor_id"] is None
    assert data["ngo_id"] is None

    vehicle = (await db.execute(select(Vehicle).where(Vehicle.id == data["driver_id"]))).scalar_one_or_none()
    assert vehicle is not None, "driver_id in register response does not match any persisted Vehicle row"
    assert vehicle.driver_id == data["user_id"]
