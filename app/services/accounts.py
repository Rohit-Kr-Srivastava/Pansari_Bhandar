"""Registration and sign-in for customers and sellers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, needs_rehash, password_problem, verify_password
from app.db.models import (
    CustomerProfile,
    SellerProfile,
    SellerStatus,
    Store,
    User,
    UserRole,
)
from app.services.checkout import PHONE_RE, normalise_phone
from app.services.product_editor import slugify, unique_slug

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


class AccountError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass
class Registration:
    full_name: str
    phone: str
    email: str
    password: str
    business_name: str = ""

    def clean(self) -> Registration:
        return Registration(
            full_name=" ".join(self.full_name.split())[:120],
            phone=normalise_phone(self.phone),
            email=self.email.strip().lower()[:254],
            password=self.password,
            business_name=" ".join(self.business_name.split())[:160],
        )


def login_key(login: str) -> str:
    """The identity find_by_login() resolves to (used as the rate-limit key)."""
    login = login.strip()
    if "@" in login:
        return "email:" + login.lower()
    return "phone:" + normalise_phone(login)


def find_by_login(db: Session, login: str) -> User | None:
    login = login.strip()
    if "@" in login:
        return db.scalar(select(User).where(func.lower(User.email) == login.lower()))
    phone = normalise_phone(login)
    return db.scalar(select(User).where(User.phone == phone)) if phone else None


def _validate(db: Session, r: Registration, seller: bool) -> None:
    errors: dict[str, str] = {}
    if len(r.full_name) < 2:
        errors["full_name"] = "Enter your name."
    if not PHONE_RE.match(r.phone):
        errors["phone"] = "Enter a 10-digit mobile number."
    if r.email and not EMAIL_RE.match(r.email):
        errors["email"] = "Enter a valid email address, or leave it empty."
    problem = password_problem(r.password)
    if problem:
        errors["password"] = problem
    if seller and len(r.business_name) < 2:
        errors["business_name"] = "Enter your shop or business name."
    if "phone" not in errors and db.scalar(select(User.id).where(User.phone == r.phone)):
        errors["phone"] = "An account with this mobile number already exists. Please sign in."
    if (
        r.email
        and "email" not in errors
        and db.scalar(select(User.id).where(func.lower(User.email) == r.email))
    ):
        errors["email"] = "An account with this email already exists. Please sign in."
    if errors:
        raise AccountError(errors)


def _flush_or_duplicate(db: Session) -> None:
    """Two people registering the same number at once: the database unique index wins."""
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccountError({"phone": "An account with these details already exists."}) from exc


def register_customer(db: Session, reg: Registration) -> User:
    r = reg.clean()
    _validate(db, r, seller=False)
    user = User(
        full_name=r.full_name,
        phone=r.phone,
        email=r.email or None,
        password_hash=hash_password(r.password),
        role=UserRole.CUSTOMER,
    )
    db.add(user)
    _flush_or_duplicate(db)
    db.add(CustomerProfile(user_id=user.id))
    db.flush()
    return user


def register_seller(db: Session, reg: Registration) -> User:
    """Creates the seller, a pending profile and an inactive store awaiting admin approval."""
    r = reg.clean()
    _validate(db, r, seller=True)
    user = User(
        full_name=r.full_name,
        phone=r.phone,
        email=r.email or None,
        password_hash=hash_password(r.password),
        role=UserRole.SELLER,
    )
    db.add(user)
    _flush_or_duplicate(db)
    profile = SellerProfile(
        user_id=user.id,
        business_name=r.business_name,
        contact_name=r.full_name,
        contact_phone=r.phone,
        status=SellerStatus.PENDING,
    )
    db.add(profile)
    db.flush()
    db.add(
        Store(
            seller_profile_id=profile.id,
            slug=unique_slug(db, Store, slugify(r.business_name) or "store"),
            name=r.business_name,
            phone=r.phone,
            is_active=False,
        )
    )
    db.flush()
    return user


def authenticate(db: Session, login: str, password: str) -> User | None:
    user = find_by_login(db, login) if login.strip() else None
    ok = verify_password(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def change_password(user: User, current: str, new: str) -> None:
    if not verify_password(user.password_hash, current):
        raise AccountError({"current_password": "Your current password is not correct."})
    problem = password_problem(new)
    if problem:
        raise AccountError({"new_password": problem})
    user.password_hash = hash_password(new)


def search_users(db: Session, role: UserRole, q: str, limit: int = 50) -> Sequence[User]:
    stmt = select(User).where(User.role == role)
    if q:
        like = f"%{q.strip()[:60]}%"
        stmt = stmt.where(
            or_(User.full_name.ilike(like), User.phone.ilike(like), User.email.ilike(like))
        )
    return db.scalars(stmt.order_by(User.created_at.desc()).limit(limit)).all()
