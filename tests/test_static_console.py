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
    end = app_js.find("async function submitComposer", start)
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

    # The composer dispatches to either chat or the web research worksheet.
    submit_start = app_js.find("async function submitComposer")
    assert submit_start != -1
    submit_composer = app_js[submit_start : submit_start + 600]
    assert 'runResearch(input.value, "web")' in submit_composer
    assert "sendMessage(event)" in submit_composer


def test_composer_web_search_switch_is_the_only_web_entrypoint() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="web-search-toggle"' in html
    assert 'type="checkbox"' in html
    assert "Web search" in html
    assert 'id="research-web"' not in html
    assert html.count('id="composer-input"') == 1
    composer_start = html.find('<form id="composer"')
    composer_end = html.find("</form>", composer_start)
    composer = html[composer_start:composer_end]
    assert composer_start != -1 and composer_end != -1
    assert 'id="web-search-toggle"' in composer
    assert 'id="web-search-toggle" checked' not in composer

    memory_start = html.find('id="panel-memory"')
    assert memory_start != -1
    memory_end = html.find("</section>", memory_start)
    assert 'id="research-web"' not in html[memory_start:memory_end]

    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'window.addEventListener("pageshow", resetComposerMode)' in app_js
    assert "toggle.checked = false" in app_js
    assert "toggle.hidden = true" in app_js
    assert "toggle.disabled = true" in app_js

    update_start = app_js.find("function updateComposerMode")
    update_end = app_js.find("function resetComposerMode", update_start)
    update_mode = app_js[update_start:update_end]
    assert "state.status?.outbound_enabled" in update_mode
    assert 'input.placeholder = webMode ? "Search the web" : "Write a message"' in update_mode
    assert 'modelLabel.textContent = webMode ? "Search the web"' in update_mode

    submit_start = app_js.find("async function submitComposer")
    submit_end = app_js.find("async function copyMarkdown", submit_start)
    submit_composer = app_js[submit_start:submit_end]
    assert 'toggle?.checked' in submit_composer
    assert 'state.status?.outbound_enabled' in submit_composer
    assert 'runResearch(input.value, "web")' in submit_composer
    assert "sendMessage(event)" in submit_composer
    assert "researchWebBtn" not in app_js
