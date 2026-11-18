from datetime import datetime
from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.auth import SignupRequest
from app.create_admin import create_admin
from app.db.auth import metadata
from app.db.connection import build_engine
from app.db.member_management import inquiries, inquiry_replies
from app.main import create_app
from migrations.member_management import migrate


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PAGE_SIZE", "1")
    url = f"sqlite:///{tmp_path / 'members.sqlite3'}"
    engine = build_engine(url)
    metadata.create_all(engine)

    password = token_urlsafe(18)
    admin = {"email": "admin@example.test", "password": password}
    create_admin(engine, SignupRequest(**admin, name="Admin", password_confirmation=password))
    engine.dispose()

    application = create_app(url)
    with TestClient(application) as anonymous, TestClient(application) as student, TestClient(application) as manager:
        login = {"email": "learner@example.test", "password": password}
        signup = student.post("/auth/signup", json={**login, "name": "Learner", "password_confirmation": password})
        assert signup.status_code == 201
        assert student.post("/auth/login", json=login).status_code == 200
        assert manager.post("/auth/login", json=admin).status_code == 200
        yield anonymous, student, manager, signup.json()["user_id"]


def test_inquiry_reply_retries_and_status_filter(sessions):
    anonymous, student, manager, _ = sessions
    path = "/admin/inquiries"
    assert anonymous.get(path).status_code == 401
    assert student.get(path).status_code == 403
    first = student.post("/me/inquiries", json={"category": "account", "title": "First", "body": "Help"})
    second = student.post("/me/inquiries", json={"category": "account", "title": "Second", "body": "More help"})
    assert first.status_code == second.status_code == 201

    page = manager.get(path, params={"status": "pending"}).json()
    assert page["items"] == [{
        "inquiry_id": second.json()["inquiry_id"], "title": "Second", "status": "pending", "category": "account",
    }]
    next_page = manager.get(path, params={"status": "pending", "cursor": page["next_cursor"]}).json()
    assert next_page["items"][0]["inquiry_id"] == first.json()["inquiry_id"]
    assert next_page["next_cursor"] is None

    detail_path = f"{path}/{first.json()['inquiry_id']}"
    reply_path = f"{detail_path}/replies"
    reply = {"request_id": "retry", "body": "Answer\nSecond line"}
    sent = manager.post(reply_path, json=reply)
    retried = manager.post(reply_path, json=reply)
    assert sent.status_code == retried.status_code == 201
    assert sent.json() == retried.json()
    assert manager.post(reply_path, json={**reply, "body": "New answer"}).status_code == 409
    assert manager.post(reply_path, json={**reply, "request_id": "another"}).status_code == 409
    detail = manager.get(detail_path).json()
    assert detail["status"] == "answered"
    assert detail["replies"] == [{"reply_id": sent.json()["reply_id"], "body": reply["body"]}]
    assert manager.get(path, params={"status": "answered"}).json()["items"][0]["inquiry_id"] == detail["inquiry_id"]


def test_member_limits_search_and_rejects_stale_update(sessions):
    _, student, manager, student_id = sessions
    path = "/admin/members"
    assert student.get(path).status_code == 403
    assert manager.get(path, params={"q": "admin"}).json()["items"] == []
    assert manager.get(path, params={"q": "LEARNER@"}).json()["items"] == [
        {"member_id": student_id, "name": "Learner", "email": "learner@example.test", "status": "active"},
    ]
    detail_path = f"{path}/{student_id}"
    detail = manager.get(detail_path).json()
    edit = {"name": "Updated", "version": detail["version"]}
    assert manager.patch(detail_path, json={**edit, "email": "changed@example.test"}).status_code == 422
    saved = manager.patch(detail_path, json=edit)
    assert saved.status_code == 200
    assert saved.json() == {"member_id": student_id, "name": "Updated", "version": detail["version"] + 1}
    assert manager.patch(detail_path, json={**edit, "name": "Stale update"}).status_code == 409
    assert manager.get(detail_path).json()["email"] == detail["email"]
    assert student.get("/me").json()["name"] == "Updated"


def test_student_can_only_read_own_inquiries_and_replies(sessions):
    other, author, manager, _ = sessions
    path = "/me/inquiries"
    assert other.get(path).status_code == 401
    assert manager.get(path).status_code == 403
    assert author.get(path).json() == {"items": [], "next_cursor": None}

    password = token_urlsafe(18)
    login = {"email": "other@example.test", "password": password}
    assert other.post("/auth/signup", json={
        **login, "name": "Other", "password_confirmation": password,
    }).status_code == 201
    assert other.post("/auth/login", json=login).status_code == 200

    first_id = author.post(path, json={"category": "account", "title": "First", "body": "Help"}).json()["inquiry_id"]
    foreign_id = other.post(path, json={"category": "learning", "title": "Private", "body": "Other"}).json()["inquiry_id"]
    second_id = author.post(path, json={"category": "other", "title": "Second", "body": "More help"}).json()["inquiry_id"]
    sent = manager.post(f"/admin/inquiries/{first_id}/replies", json={"request_id": "reply", "body": "Answer"})
    assert sent.status_code == 201

    page = author.get(path).json()
    assert [(item["inquiry_id"], item["status"]) for item in page["items"]] == [(second_id, "pending")]
    assert datetime.fromisoformat(page["items"][0]["created_at"]).tzinfo is not None
    next_page = author.get(path, params={"cursor": page["next_cursor"]}).json()
    assert [(item["inquiry_id"], item["status"]) for item in next_page["items"]] == [(first_id, "answered")]
    assert next_page["next_cursor"] is None
    assert author.get(path, params={"cursor": foreign_id}).status_code == 422
    assert author.get(f"{path}/{foreign_id}").status_code == 404
    assert other.get(f"{path}/{first_id}").status_code == 404
    detail = author.get(f"{path}/{first_id}").json()
    assert detail["body"] == "Help"
    assert detail["status"] == "answered"
    assert detail["replies"] == [{"reply_id": sent.json()["reply_id"], "body": "Answer"}]


def test_members_sort_entire_filtered_list_before_paging(sessions):
    anonymous, _, manager, first_id = sessions
    password = token_urlsafe(18)
    signup = anonymous.post("/auth/signup", json={
        "email": "second@example.test", "name": "Second",
        "password": password, "password_confirmation": password,
    })
    assert signup.status_code == 201
    second_id = signup.json()["user_id"]
    path = "/admin/members"

    for sort, expected in (("newest", (second_id, first_id)), ("oldest", (first_id, second_id))):
        params = {"q": "@example.test", "sort": sort}
        page = manager.get(path, params=params).json()
        assert page["items"][0]["member_id"] == expected[0]
        assert page["next_cursor"]
        second_page = manager.get(path, params={**params, "cursor": page["next_cursor"]}).json()
        assert second_page["items"][0]["member_id"] == expected[1]
        assert second_page["next_cursor"] is None
    assert manager.get(path, params={"sort": "unsupported"}).status_code == 422


def test_migration_retains_existing_members(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO users (id) VALUES ('existing')")
    migrate(engine)
    migrate(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version FROM users WHERE id = 'existing'").scalar_one() == 1
        assert inspect(connection).has_table(inquiries.name)
        assert inspect(connection).has_table(inquiry_replies.name)
    engine.dispose()
