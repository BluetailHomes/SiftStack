import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import scraper


def test_has_placeholder_credentials_detects_placeholder_values(monkeypatch):
    monkeypatch.setattr(scraper.config, "NOTICE_SITE_EMAIL", "your_email@example.com")
    monkeypatch.setattr(scraper.config, "NOTICE_SITE_PASSWORD", "your_password_here")

    assert scraper._has_placeholder_credentials() is True


def test_has_placeholder_credentials_accepts_real_values(monkeypatch):
    monkeypatch.setattr(scraper.config, "NOTICE_SITE_EMAIL", "real.user@example.com")
    monkeypatch.setattr(scraper.config, "NOTICE_SITE_PASSWORD", "strong-password")

    assert scraper._has_placeholder_credentials() is False
