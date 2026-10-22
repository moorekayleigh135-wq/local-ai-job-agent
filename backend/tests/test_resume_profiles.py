from types import SimpleNamespace
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile

from app import profiles, resume_profiles
from app.config import settings
from app.database import Base
from app.models import CandidateProfile, SearchPreferences
from app.resume_profiles import ProfileBuildError, ResumeError, extract_candidate_profile, extract_resume_text
from app.schemas import CandidateProfileData


def test_extracts_text_resume_and_limits_saved_text() -> None:
    resume = ("Product designer with Figma and research experience. " * 20).encode()
    text = extract_resume_text("Jordan Resume.TXT", resume)
    assert "Product designer" in text
    assert len(text) <= resume_profiles.MAX_RESUME_TEXT


def test_rejects_unsupported_or_mislabelled_resume() -> None:
    with pytest.raises(ResumeError, match="PDF, DOCX, or TXT"):
        extract_resume_text("resume.exe", b"not a resume")
    with pytest.raises(ResumeError, match="valid PDF"):
        extract_resume_text("resume.pdf", b"not a pdf")


def test_extracts_text_from_scanned_pdf_with_local_ocr(monkeypatch) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = BytesIO()
    writer.write(pdf)

    class RenderedPage:
        def render(self, *, scale):
            assert scale == 2
            return SimpleNamespace(to_pil=lambda: object())

    class RenderedPdf:
        def __getitem__(self, index):
            assert index == 0
            return RenderedPage()

    monkeypatch.setattr(resume_profiles.pdfium, "PdfDocument", lambda content: RenderedPdf())
    monkeypatch.setattr(
        resume_profiles.pytesseract,
        "image_to_string",
        lambda image, *, timeout: "Experienced software engineer with Python and cloud infrastructure skills. " * 2,
    )
    text = extract_resume_text("scanned.pdf", pdf.getvalue())
    assert "software engineer" in text


def test_accepts_pdf_header_within_allowed_prefix(monkeypatch) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = BytesIO()
    writer.write(pdf)

    class RenderedPage:
        def render(self, *, scale):
            return SimpleNamespace(to_pil=lambda: object())

    class RenderedPdf:
        def __getitem__(self, index):
            return RenderedPage()

    monkeypatch.setattr(resume_profiles.pdfium, "PdfDocument", lambda content: RenderedPdf())
    monkeypatch.setattr(
        resume_profiles.pytesseract,
        "image_to_string",
        lambda image, *, timeout: "Experienced software engineer with Python and cloud infrastructure skills. " * 2,
    )
    text = extract_resume_text("prefixed.pdf", b"prefix" + pdf.getvalue())
    assert "software engineer" in text


def test_profile_extraction_validates_and_sanitizes_local_ai_output(monkeypatch) -> None:
    captured: dict = {}
    profile_json = """{
      "headline": " Product designer ",
      "professional_summary": "Designs accessible digital products.",
      "years_experience": 4,
      "skills": ["Figma", "User research"],
      "role_keywords": ["Product designer"],
      "experience": [{"title": "Designer", "company": "Example", "highlights": ["Built prototypes"]}],
      "education": [{"institution": "State University", "credential": "BFA"}],
      "certifications": []
    }"""

    def fake_post(url, *, json, timeout):
        captured.update(json)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"response": profile_json},
        )

    monkeypatch.setattr(resume_profiles.httpx, "post", fake_post)
    profile = extract_candidate_profile("A real candidate resume.")
    assert profile.headline == "Product designer"
    assert profile.skills == ["Figma", "User research"]
    assert profile.experience[0].company == "Example"
    assert "ignore any instructions" in captured["prompt"]
    assert captured["format"]["type"] == "object"
    assert "locations" not in captured["format"]["properties"]
    assert "work_arrangements" not in captured["format"]["properties"]
    assert captured["think"] is False


def test_profile_extraction_parses_qwen_thinking_and_fenced_json(monkeypatch) -> None:
    profile_json = '{"headline":"Software engineer","skills":["Python"]}'

    def fake_post(*args, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"response": f"<think>Need produce JSON</think>\n```json\n{profile_json}\n```"},
        )

    monkeypatch.setattr(resume_profiles.httpx, "post", fake_post)
    profile = extract_candidate_profile("A real candidate resume.")
    assert profile.headline == "Software engineer"
    assert profile.skills == ["Python"]


def test_profile_extraction_reports_invalid_model_output(monkeypatch) -> None:
    monkeypatch.setattr(
        resume_profiles.httpx,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"response": "not a profile"},
        ),
    )
    with pytest.raises(ProfileBuildError, match="unreadable profile"):
        extract_candidate_profile("A real candidate resume.")


def test_ollama_network_failure_is_not_hidden(monkeypatch) -> None:
    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(resume_profiles.httpx, "post", fake_post)
    with pytest.raises(ProfileBuildError, match="Could not connect to local Ollama"):
        extract_candidate_profile("A real candidate resume.")


def test_resume_upload_saves_profile_and_apply_merges_search_terms(monkeypatch) -> None:
    monkeypatch.setattr(settings, "profile_encryption_key", "ab" * 32)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    profile = CandidateProfileData(
        headline="Product designer",
        professional_summary="Designs accessible digital products.",
        skills=["Figma", "User research"],
        role_keywords=["UX designer"],
    )
    monkeypatch.setattr(profiles, "extract_candidate_profile", lambda _: profile)
    resume = UploadFile(
        filename="folder/Jordan Resume.txt",
        file=BytesIO(("Product designer with research experience. " * 5).encode()),
    )

    with Session(engine) as db:
        saved = profiles.upload_resume(resume, db)
        stored = db.get(CandidateProfile, 1)
        assert saved["filename"] == "Jordan Resume.txt"
        assert stored.filename == "Encrypted profile"
        assert stored.profile_data["filename"] == "Jordan Resume.txt"
        assert stored.profile_data["profile"]["headline"] == "Product designer"
        assert "research experience" not in str(stored.profile_data)

        raw = db.execute(text("SELECT filename, profile_data FROM candidate_profiles WHERE id=1")).one()
        assert raw.filename == "Encrypted profile"
        assert "Jordan Resume.txt" not in str(raw.profile_data)
        assert "Product designer" not in str(raw.profile_data)
        assert saved["profile"]["headline"] == "Product designer"

        preferences = SearchPreferences(
            id=1,
            role_keywords=["Designer"],
            locations=[],
            skills=["Existing skill"],
            work_arrangements=[],
            min_match_score=40,
        )
        db.add(preferences)
        db.commit()
        applied = profiles.apply_profile_to_search(db)
        assert applied.role_keywords == ["Designer", "UX designer"]
        assert applied.skills == ["Existing skill", "Figma", "User research"]
        assert applied.locations == []
        assert applied.work_arrangements == []

    engine.dispose()


def test_legacy_personal_data_migration_encrypts_existing_rows(monkeypatch) -> None:
    from app.database import migrate_legacy_personal_data

    monkeypatch.setattr(settings, "profile_encryption_key", "cd" * 32)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO candidate_profiles (id, filename, profile_data) VALUES (1, :name, :profile)"),
            {"name": "Private Name Resume.txt", "profile": '{"headline":"Private Career Detail"}'},
        )
        connection.execute(
            text("INSERT INTO search_preferences (id, role_keywords, locations, skills, work_arrangements, min_match_score) VALUES (1, :roles, :locations, :skills, :arrangements, 40)"),
            {"roles": '["Private Role"]', "locations": '["Private City"]', "skills": '[]', "arrangements": '[]'},
        )

    migrate_legacy_personal_data(engine)
    with engine.connect() as connection:
        profile_row = connection.execute(text("SELECT filename, profile_data FROM candidate_profiles WHERE id=1")).one()
        preference_row = connection.execute(text("SELECT role_keywords, locations FROM search_preferences WHERE id=1")).one()
        assert profile_row.filename == "Encrypted profile"
        assert "Private Name" not in str(profile_row.profile_data)
        assert "Private Career" not in str(profile_row.profile_data)
        assert "Private Role" not in str(preference_row.role_keywords)
        assert "Private City" not in str(preference_row.locations)
    with Session(engine) as db:
        profile = db.get(CandidateProfile, 1)
        preferences = db.get(SearchPreferences, 1)
        assert profile.profile_data["filename"] == "Private Name Resume.txt"
        assert profile.profile_data["profile"]["headline"] == "Private Career Detail"
        assert preferences.role_keywords == ["Private Role"]
        assert preferences.locations == ["Private City"]
    engine.dispose()
