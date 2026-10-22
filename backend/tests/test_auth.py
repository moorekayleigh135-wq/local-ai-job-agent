from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from app import auth
from app.database import Base
from app.models import AuthSession


def make_request(token: str | None = None) -> Request:
    headers = [(b"host", b"testserver")]
    if token:
        headers.append((b"cookie", f"{auth.COOKIE_NAME}={token}".encode()))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth",
            "raw_path": b"/auth",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )


def cookie_token(response: Response) -> str:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    morsel = cookie[auth.COOKIE_NAME]
    assert morsel["httponly"]
    assert morsel["samesite"] == "strict"
    return morsel.value


def test_first_account_login_password_change_and_logout(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(auth, "SessionLocal", sessionmaker(bind=engine, autoflush=False))

    registration_response = Response()
    created = auth.create_account(
        auth.AccountCreate(username="LocalUser", password="a-strong-test-password"),
        make_request(),
        registration_response,
    )
    assert created == {"username": "LocalUser"}
    token = cookie_token(registration_response)
    assert auth.auth_status(make_request(token)) == {
        "setup_required": False,
        "authenticated": True,
        "username": "LocalUser",
    }
    with Session(engine) as db:
        saved_hash = db.scalar(select(AuthSession.token_hash))
        assert saved_hash == auth.token_digest(token)
        assert saved_hash != token

    login_response = Response()
    auth.login(
        auth.LoginRequest(username="localuser", password="a-strong-test-password"),
        make_request(),
        login_response,
    )
    login_token = cookie_token(login_response)

    changed_response = Response()
    result = auth.change_password(
        auth.PasswordChange(
            current_password="a-strong-test-password",
            new_password="another-strong-test-password",
        ),
        make_request(login_token),
        changed_response,
    )
    assert result == {"message": "Password updated."}
    changed_token = cookie_token(changed_response)
    assert auth.auth_status(make_request(login_token))["authenticated"] is False
    assert auth.auth_status(make_request(changed_token))["authenticated"] is True

    auth.logout(make_request(changed_token), Response())
    assert auth.auth_status(make_request(changed_token))["authenticated"] is False
    with pytest.raises(HTTPException) as error:
        auth.create_account(
            auth.AccountCreate(username="SecondUser", password="another-strong-test-password"),
            make_request(),
            Response(),
        )
    assert error.value.status_code == 409
    engine.dispose()
