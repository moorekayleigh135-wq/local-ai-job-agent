"""Single-user local account setup, login, and password management."""

from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AuthAccount, AuthSession

router = APIRouter(prefix="/auth", tags=["local account"])
password_hasher = PasswordHash.recommended()
COOKIE_NAME = "jobharbor_session"
SESSION_DAYS = 30


class AccountCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_utc_naive() -> datetime:
    """Use naive UTC values for consistent MySQL DATETIME comparisons."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def issue_session(db: Session, account: AuthAccount, response: Response, request: Request) -> None:
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        account_id=account.id,
        token_hash=token_digest(token),
        expires_at=current_utc_naive() + timedelta(days=SESSION_DAYS),
    )
    db.add(session)
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def account_for_token(db: Session, token: str | None) -> AuthAccount | None:
    if not token:
        return None
    return db.scalar(
        select(AuthAccount)
        .join(AuthSession, AuthSession.account_id == AuthAccount.id)
        .where(
            AuthSession.token_hash == token_digest(token),
            AuthSession.expires_at > current_utc_naive(),
        )
    )


@router.get("/status")
def auth_status(request: Request) -> dict[str, object]:
    with SessionLocal() as db:
        account = db.get(AuthAccount, 1)
        authenticated = account_for_token(db, request.cookies.get(COOKIE_NAME))
        return {
            "setup_required": account is None,
            "authenticated": authenticated is not None,
            "username": authenticated.username if authenticated else None,
        }


@router.post("/register", status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, request: Request, response: Response) -> dict[str, str]:
    """Create the first local account; this installation intentionally has one owner."""
    with SessionLocal() as db:
        if db.get(AuthAccount, 1) is not None:
            raise HTTPException(status_code=409, detail="A local account already exists. Sign in instead.")
        account = AuthAccount(
            id=1,
            username=payload.username.strip(),
            password_hash=password_hasher.hash(payload.password),
        )
        db.add(account)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Account setup has already been completed.") from exc
        issue_session(db, account, response, request)
        return {"username": account.username}


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    with SessionLocal() as db:
        account = db.get(AuthAccount, 1)
        valid = bool(
            account
            and account.username.casefold() == payload.username.strip().casefold()
            and password_hasher.verify(payload.password, account.password_hash)
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Username or password is incorrect.")
        db.query(AuthSession).filter(
            AuthSession.expires_at <= current_utc_naive()
        ).delete(synchronize_session=False)
        issue_session(db, account, response, request)
        return {"username": account.username}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with SessionLocal() as db:
            db.query(AuthSession).filter(AuthSession.token_hash == token_digest(token)).delete()
            db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return response


@router.post("/password")
def change_password(payload: PasswordChange, request: Request, response: Response) -> dict[str, str]:
    with SessionLocal() as db:
        account = account_for_token(db, request.cookies.get(COOKIE_NAME))
        if account is None:
            raise HTTPException(status_code=401, detail="Sign in to change your password.")
        if not password_hasher.verify(payload.current_password, account.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
        account.password_hash = password_hasher.hash(payload.new_password)
        db.query(AuthSession).filter(AuthSession.account_id == account.id).delete()
        db.commit()
        issue_session(db, account, response, request)
        return {"message": "Password updated."}
