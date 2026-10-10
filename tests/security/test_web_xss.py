"""R23 XSS: untrusted Evertrace data (names, summaries, companies, search titles) is
always escaped, templates never opt out of autoescaping, and the page assets
contain nothing the CSP would have to allow inline."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.domain.models import CompanyRef, FounderRecord, RunTrigger, SavedSearch, SearchFilter, Signal
from app.web import security
from app.web.rendering import TEMPLATE_DIR, companies_house_link, linkedin_link, templates
from tests.fakes import make_signal
from tests.web import conftest as web_fixtures
from tests.web.conftest import (
    NOW,
    SEARCH,
    WebClient,
    World,
)

# The dashboard fixtures (tests/security has no conftest of its own).
anon = web_fixtures.anon
client = web_fixtures.client
world = web_fixtures.world

STATIC_DIR = Path(security.__file__).resolve().parent / "static"

XSS_FIRST = "<script>alert(1)</script>"
XSS_LAST = "<img src=x onerror=alert(2)>"
XSS_SUMMARY = '<script>alert("summary")</script><a href="javascript:alert(3)">click</a>'
XSS_COMPANY = '"><script>alert(4)</script>'
XSS_SIGNAL = "<svg onload=alert(5)>"
XSS_COUNTRY = "<script>alert(6)</script>"
XSS_SEARCH = "<script>alert(7)</script>"

RAW_MARKERS = ("<script>alert", "<img src=x", "<svg onload", 'href="javascript:', '"><script')


def evil(n: int, **overrides: object) -> Signal:
    values: dict[str, object] = {
        "first_name": XSS_FIRST,
        "last_name": XSS_LAST,
        "summary": XSS_SUMMARY,
        "signal_type": XSS_SIGNAL,
        "company": CompanyRef(name=XSS_COMPANY),
    }
    values.update(overrides)
    return make_signal(n, **values)


def assert_escaped(page: str, where: str) -> None:
    for marker in RAW_MARKERS:
        assert marker not in page, (where, marker)


@pytest.fixture
def evil_ids(world: World, client: WebClient) -> dict[str, int]:
    ids = world.seed(
        evil(1, email="e1@co1.example"),  # DRAFTED
        evil(2),  # NEEDS_EMAIL
        evil(3, country=XSS_COUNTRY),  # EXCLUDED (location), detail contains the country
        evil(4, email="e4@co4.example"),  # -> SENT
        evil(5, email="e5@co5.example"),  # -> REPLIED
        evil(6, email="e6@co6.example"),  # -> DECK_RECEIVED
    )
    out = world.services.outreach
    for n in (4, 5, 6):
        out.mark_sent(ids[f"sig_test000{n}"])
    for n in (5, 6):
        out.mark_replied(ids[f"sig_test000{n}"])
    out.mark_deck_received(ids["sig_test0006"])
    return ids


def test_malicious_founder_data_is_escaped_on_every_page(
    client: WebClient, world: World, evil_ids: dict[str, int]
) -> None:
    world.clock.advance(days=15)  # makes the SENT founder a follow-up on the overview
    client.login()
    pages = ["/", "/drafts", "/needs-email", "/excluded", "/pipeline", "/runs"]
    pages += [f"/outreach/{oid}" for oid in evil_ids.values()]
    for path in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        assert_escaped(response.text, path)
    # The escaped text is really there (so the check above is meaningful).
    for path in ("/drafts", "/needs-email", "/excluded", "/pipeline", "/"):
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get(path).text, path
    detail = client.get(f"/outreach/{evil_ids['sig_test0001']}").text
    assert "&lt;img src=x onerror=alert(2)&gt;" in detail
    assert "&lt;svg onload=alert(5)&gt;" in detail
    assert "&#34;&gt;&lt;script&gt;alert(4)&lt;/script&gt;" in detail
    assert "&lt;script&gt;alert(6)&lt;/script&gt;" in client.get("/excluded").text


def test_draft_body_cannot_break_out_of_its_textarea(
    client: WebClient, world: World, evil_ids: dict[str, int]
) -> None:
    body = "Hi {first_name}</textarea><script>alert(8)</script>"
    client.post("/template", {"subject": "Hello {full_name}", "body": body, "rerender": "true"})
    for path in ("/template", "/drafts", f"/outreach/{evil_ids['sig_test0001']}"):
        page = client.get(path).text
        assert "<script>alert(8)" not in page, path
        assert_escaped(page, path)
    assert "&lt;/textarea&gt;&lt;script&gt;alert(8)" in client.get("/drafts").text


def test_malicious_search_title_and_flash_are_escaped(client: WebClient, world: World) -> None:
    world.catalog.searches.append(
        SavedSearch(
            external_id="sch_evil", title=XSS_SEARCH, filters=(SearchFilter(key="country", values=("UK",)),)
        )
    )
    (config,) = world.services.searches.sync_catalog()
    page = client.get("/searches").text
    assert_escaped(page, "/searches")
    assert "&lt;script&gt;alert(7)&lt;/script&gt;" in page
    # The title is echoed in a flash message (signed cookie) after saving.
    response = client.post(
        f"/searches/{config.id}",
        {"enabled": "true", "location_policy": "uk_outside_london", "lookback_days": "30"},
    )
    flashed = client.get(response.headers["location"]).text
    assert_escaped(flashed, "flash")
    assert "&lt;script&gt;alert(7)&lt;/script&gt;" in flashed


def test_user_input_echoed_in_validation_errors_is_escaped(client: WebClient) -> None:
    response = client.post(
        "/searches/new",
        {"title": XSS_SEARCH, "industry": ["<script>alert(9)</script>"], "lookback_days": "30"},
    )
    assert response.status_code == 400
    assert "<script>alert(9)" not in response.text and "<script>alert(7)" not in response.text
    assert "&lt;script&gt;alert(9)&lt;/script&gt;" in response.text


def test_malicious_run_errors_are_escaped(client: WebClient, world: World) -> None:
    world.enable_search()
    world.signals.fail_for.add(SEARCH.filters)
    world.services.origination.run(RunTrigger.CRON)
    assert_escaped(client.get("/runs").text, "/runs")


def test_forged_flash_cookie_is_ignored(client: WebClient) -> None:
    client.client.cookies.set("dsw_flash", "W1siZXJyb3IiLCI8c2NyaXB0PiJdXQ.forged.sig")
    page = client.get("/").text
    assert "flash-error" not in page and "<script>" not in page


# --------------------------------------------------------------------------- #
# Template and asset hygiene
# --------------------------------------------------------------------------- #


def test_autoescape_is_on_for_every_template() -> None:
    env = templates.env
    assert env.autoescape is True
    for name in env.list_templates():
        assert env.get_template(name).environment.autoescape is True


def _template_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in TEMPLATE_DIR.glob("*.html")}


def test_templates_never_mark_data_safe() -> None:
    sources = _template_sources()
    assert len(sources) >= 12
    for name, text in sources.items():
        assert not re.search(r"\|\s*safe\b", text), name
        assert "Markup" not in text and "autoescape false" not in text, name


def test_templates_have_no_inline_scripts_styles_or_handlers() -> None:
    pages = dict(_template_sources())
    pages["_INTERNAL_ERROR_PAGE"] = security._INTERNAL_ERROR_PAGE
    pages["_UNAUTHORISED_PAGE"] = security._UNAUTHORISED_PAGE
    for name, text in pages.items():
        for tag in re.findall(r"<script\b[^>]*>", text, re.I):
            assert 'src="/static/' in tag, (name, tag)
        assert not re.search(r"\sstyle\s*=", text, re.I), name
        assert not re.search(r"<style\b", text, re.I), name
        assert not re.search(r"\son[a-z]+\s*=", text, re.I), name
        assert "javascript:" not in text.lower(), name
        assert "http://" not in text and "https://" not in text, name  # no external assets


def test_external_links_open_safely() -> None:
    macros = (TEMPLATE_DIR / "_macros.html").read_text(encoding="utf-8")
    for tag in re.findall(r'<a [^>]*target="_blank"[^>]*>', macros):
        assert 'rel="noopener noreferrer"' in tag


def test_static_assets_avoid_dangerous_dom_apis() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert api not in js, api
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css


def _founder(**overrides: object) -> FounderRecord:
    values: dict[str, object] = {
        "evertrace_signal_id": "sig_x",
        "first_name": "A",
        "last_name": "B",
        "discovered_at": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    return FounderRecord(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("slug", "ok"),
    [
        ("alex-testfounder-1", True),
        ("jos%C3%A9-example", True),
        ('x" onmouseover="alert(1)', False),
        ("x/../../evil", False),
        ("a?b=c", False),
        (None, False),
    ],
)
def test_linkedin_links_are_whitelisted(slug: str | None, ok: bool) -> None:
    link = linkedin_link(_founder(linkedin_slug=slug))
    assert (link is not None) is ok
    if ok:
        assert link == f"https://www.linkedin.com/in/{slug}"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://find-and-update.company-information.service.gov.uk/company/01234567", True),
        ("http://find-and-update.company-information.service.gov.uk/company/01234567", False),
        ("https://find-and-update.company-information.service.gov.uk.evil.example/x", False),
        ("javascript:alert(1)", False),
        ('https://find-and-update.company-information.service.gov.uk/"onmouseover=x', False),
        (None, False),
    ],
)
def test_companies_house_links_are_whitelisted(url: str | None, ok: bool) -> None:
    assert (companies_house_link(_founder(company_registry_url=url)) is not None) is ok
