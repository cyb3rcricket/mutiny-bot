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
        "research-notes",
        "research-lab",
        "research-copy",
        "research-download",
    ):
        assert marker in html
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "#191b1a" in css
    assert "#222522" in css
    assert "#e5e0d6" in css
    assert "#a9b28f" in css
    assert "prefers-reduced-motion" in css


def test_research_click_does_not_post_chat_messages() -> None:
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/tools/research/runs" in app_js
    assert 'mode: "closed"' in app_js

    # Extract the runResearch function body to verify it does not call the chat messages route
    start = app_js.find("async function runResearch")
    assert start != -1
    end = app_js.find("async function copyMarkdown", start)
    assert end != -1
    run_research_code = app_js[start:end]
    assert "/api/tools/research/runs" in run_research_code
    assert "/messages" not in run_research_code
    assert "sendmessage" not in run_research_code.lower()

    # The research button triggers runResearch, not chat submission
    btn_listener_start = app_js.find('document.querySelector("#research-notes").addEventListener')
    assert btn_listener_start != -1
    btn_listener = app_js[btn_listener_start : btn_listener_start + 300]
    assert "runResearch" in btn_listener
    assert "sendMessage" not in btn_listener

    # Verify composer Send form is still dedicated to chat sendMessage
    composer_listener = app_js[app_js.find('document.querySelector("#composer")') :]
    assert "sendMessage" in composer_listener[:200]
    assert "runResearch" not in composer_listener[:200]
