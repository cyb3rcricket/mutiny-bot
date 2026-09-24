"""The console page is local and exposes the editorial workflow."""

from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "web" / "static"


def test_page_has_no_remote_assets() -> None:
    for path in STATIC.iterdir():
        text = path.read_text(encoding="utf-8").lower()
        assert "http://" not in text
        assert "https://" not in text
        assert "cdn" not in text


def test_page_exposes_console_regions() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "thread-list",
        "composer",
        "remember-form",
        "job-form",
        "run-briefing",
        "privacy-list",
        "clear-history",
        "reset-context",
    ):
        assert marker in html
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "#191b1a" in css
    assert "#222522" in css
    assert "#e5e0d6" in css
    assert "#a9b28f" in css
    assert "prefers-reduced-motion" in css
