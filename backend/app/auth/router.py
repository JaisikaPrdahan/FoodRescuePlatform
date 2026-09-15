"""
Auth router — the foundation everyone else's login screens depend on, per §2.

POST /auth/register — create a user + role-specific profile row (Donor/NGO/Vehicle)
POST /auth/login     — email/password -> access + refresh token pair
POST /auth/refresh   — refresh_token -> new access_token + rotated refresh_token
GET  /auth/me         — current user profile + linked donor_id / ngo_id / driver_id

No Authorization header on register/login/refresh, per Global Conventions §1.
"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import create_access_token, create_refresh_token, get_current_user
from app.auth.schemas import LoginRequest, RefreshRequest, RegisterRequest
from app.core.config import settings
from app.core.database import get_db
from app.core.envelope import api_error, envelope
from app.core.ids import new_id
from app.models import NGO, Donor, NGOFoodCategory, User, UserRole, Vehicle

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _location_str(lat: float | None, lng: float | None) -> str:
    return f"{lat},{lng}" if lat is not None and lng is not None else ""


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise api_error(409, "EMAIL_TAKEN", "An account with this email already exists.", "email")

    user_id = new_id("usr")
    user = User(
        id=user_id,
        name=body.name,
        email=body.email,
        phone=body.phone,
        password_hash=pwd_context.hash(body.password),
        role=UserRole[body.role],
        created_at=datetime.utcnow(),
    )
    db.add(user)
    # Flush now so the users row exists before role-specific child rows are
    # added below — without a declared relationship(), the unit-of-work has
    # no dependency graph to order same-commit inserts by FK, and falls back
    # to alphabetical table order (donors/ngo_food_categories < users),
    # which violates the FK on Postgres. SQLite doesn't enforce FKs by
    # default, so this was invisible there.
    await db.flush()

    verification_status = "N/A"
    donor_id = ngo_id = driver_id = None

    if body.role == "DONOR":
        if not body.organisation_name or not body.address:
            raise api_error(
                400, "MISSING_FIELD",
                "organisation_name and address are required to register a donor.",
                "organisation_name",
            )
        donor_id = new_id("don_org")
        db.add(Donor(
            id=donor_id,
            user_id=user_id,
            organisation_name=body.organisation_name,
            address=body.address,
            location=_location_str(body.latitude, body.longitude),
            contact_person=body.contact_person or body.name,
            verification_status="PENDING",
            daily_waste_category=body.daily_waste_category or "MIXED",
        ))
        verification_status = "PENDING"

    elif body.role == "NGO":
        if not body.organisation_name or not body.address or body.storage_capacity_kg is None:
            raise api_error(
                400, "MISSING_FIELD",
                "organisation_name, address, and storage_capacity_kg are required to register an NGO.",
                "organisation_name",
            )
        ngo_id = new_id("ngo")
        db.add(NGO(
            id=ngo_id,
            user_id=user_id,
            organisation_name=body.organisation_name,
            address=body.address,
            location=_location_str(body.latitude, body.longitude),
            storage_capacity_kg=body.storage_capacity_kg,
            available_capacity_kg=body.storage_capacity_kg,
            operating_start=body.operating_start or "08:00",
            operating_end=body.operating_end or "20:00",
        ))
        for category in (body.accepted_categories or []):
            db.add(NGOFoodCategory(ngo_id=ngo_id, food_category=category, accepted=True))
        verification_status = "PENDING"

    elif body.role == "DRIVER":
        driver_id = new_id("veh")
        db.add(Vehicle(
            id=driver_id,
            driver_id=user_id,
            capacity_kg=body.vehicle_capacity_kg or 0.0,
            current_location="",
            availability_status="OFFLINE",
        ))

    await db.commit()

    return envelope({
        "user_id": user_id,
        "role": body.role,
        "verification_status": verification_status,
        "donor_id": donor_id,
        "ngo_id": ngo_id,
        "driver_id": driver_id,
    })


@router.post("/login")
async def login(body: LoginRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None or not pwd_context.verify(body.password, user.password_hash):
        raise api_error(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")

    access_token = create_access_token({"sub": user.id, "role": user.role.value})
    refresh_token = create_refresh_token({"sub": user.id})

    return envelope({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "phone": user.phone,
            "role": user.role.value,
        },
    })


@router.post("/refresh")
async def refresh(body: RefreshRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        payload = jwt.decode(body.refresh_token, settings.JWT_REFRESH_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise api_error(401, "INVALID_TOKEN", "Refresh token is invalid or expired.")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise api_error(401, "INVALID_TOKEN", "Refresh token is invalid or expired.")

    return envelope({
        "access_token": create_access_token({"sub": user.id, "role": user.role.value}),
        "refresh_token": create_refresh_token({"sub": user.id}),
        "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    })


@router.get("/me")
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    donor_id = ngo_id = driver_id = None

    if user.role == UserRole.DONOR:
        r = await db.execute(select(Donor.id).where(Donor.user_id == user.id))
        donor_id = r.scalar_one_or_none()
    elif user.role == UserRole.NGO:
        r = await db.execute(select(NGO.id).where(NGO.user_id == user.id))
        ngo_id = r.scalar_one_or_none()
    elif user.role == UserRole.DRIVER:
        r = await db.execute(select(Vehicle.id).where(Vehicle.driver_id == user.id))
        driver_id = r.scalar_one_or_none()

    return envelope({
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role.value,
        "donor_id": donor_id,
        "ngo_id": ngo_id,
        "driver_id": driver_id,
    })
