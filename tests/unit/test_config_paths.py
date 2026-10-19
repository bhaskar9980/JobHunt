"""Personal paths live in .env, never in the committed settings/profile files."""

from pathlib import Path

import pytest

from jobhunt.config import ConfigError, Settings, load_profile


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        db_path=tmp_path / "data" / "jobs.db",
        raw={"paths": {"resume_pdf": "data/resume/resume.pdf"}},
    )


def test_env_override_wins_over_settings_path(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.delenv("JOBHUNT_RESUME_PDF", raising=False)
    assert settings.path("resume_pdf") == tmp_path / "data" / "resume" / "resume.pdf"
    monkeypatch.setenv("JOBHUNT_RESUME_PDF", str(tmp_path / "cv.pdf"))
    assert settings.path("resume_pdf") == tmp_path / "cv.pdf"


def test_missing_profile_points_at_the_example(tmp_path):
    settings = make_settings(tmp_path)
    (tmp_path / "config").mkdir()
    with pytest.raises(ConfigError, match="profile.example.yaml"):
        load_profile(settings)
