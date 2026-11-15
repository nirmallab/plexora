"""Bug reports and feature requests: the server's context and relay
(server/routes/feedback_routes.py), the navbar and Help menu that open the form,
and what never happens to the person's email."""

from __future__ import annotations

import io
import json
import logging

import pytest

import plexora
from plexora.browser import routes as browser_routes
from plexora.licensing import LICENSING

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.fixture
def client():
    with plexora.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def platform(monkeypatch):
    """`Licensing.feedback` and the client's file upload, answered here."""
    sent = {"reports": [], "files": []}

    def feedback(report):
        sent["reports"].append(report)
        return {"id": "fb_0123456789abcdef", "upload": {"token": "123.abc", "expires_at": 123}}

    def attach(report_id, token, name, data):
        sent["files"].append((report_id, token, name, data))
        return {"n": len(sent["files"]) - 1}

    monkeypatch.setattr(LICENSING, "feedback", feedback, raising=False)
    monkeypatch.setattr(LICENSING, "feedback_attachment", attach, raising=False)
    return sent


def _submit(client, report, files=(), **kwargs):
    data = {"report": json.dumps(report)}
    if files:
        data["files"] = [(io.BytesIO(body), name) for name, body in files]
    return client.post("/feedback/submit", data=data, content_type="multipart/form-data", **kwargs)


REPORT = {"kind": "bug", "description": "The channel list goes blank after reopening.",
          "diagnostics": {"app_version": "0.0.57", "mode": "desktop"}, "email": "pat@lab.example.org",
          "certificate": "smuggled", "product": "scimappro"}


def test_the_context_says_signed_in_and_which_build_and_never_an_email(client, pro_license, monkeypatch):
    monkeypatch.setattr(LICENSING, "feedback", lambda report: {}, raising=False)
    reply = client.get("/feedback/context")
    body = reply.get_json()
    assert reply.status_code == 200 and reply.headers["Cache-Control"] == "no-store"
    assert body["signed_in"] is True and body["relay"] is True and body["website"] is False
    assert body["version"] == plexora.__version__
    assert body["build"].startswith("source") or len(body["build"]) == 7
    assert "@" not in json.dumps(body)


def test_a_free_install_is_not_signed_in(client):
    assert client.get("/feedback/context").get_json()["signed_in"] is False


def test_the_relay_sends_the_report_then_each_file(client, platform):
    reply = _submit(client, REPORT, files=[("shot.png", PNG), ("plexora.log", b"line\n")])
    body = reply.get_json()
    assert reply.status_code == 201, body
    assert body["id"] == "fb_0123456789abcdef"
    assert body["files"] == [{"name": "shot.png", "ok": True}, {"name": "plexora.log", "ok": True}]
    assert body["upload"]["token"] == "123.abc", "so the page can send a failed file again"
    # Only the report's own fields pass: never a certificate or product the page made up.
    assert platform["reports"] == [{key: REPORT[key] for key in ("kind", "description", "diagnostics", "email")}]
    assert [(name, data) for _, _, name, data in platform["files"]] == [("shot.png", PNG), ("plexora.log", b"line\n")]
    assert reply.headers["Cache-Control"] == "no-store"


def test_a_file_that_fails_is_named_and_the_report_still_stands(client, platform, monkeypatch):
    from biocognia.errors import ServerError

    def refuse(report_id, token, name, data):
        raise ServerError("shot.png is not a PNG, JPEG or WebP image.", code="invalid_request")

    monkeypatch.setattr(LICENSING, "feedback_attachment", refuse, raising=False)
    body = _submit(client, REPORT, files=[("shot.png", b"not an image")]).get_json()
    assert body["id"] and body["files"][0]["ok"] is False
    assert "not a PNG" in body["files"][0]["error"]


def test_what_the_platform_refuses_stays_a_refusal_and_the_rest_falls_back(client, platform, monkeypatch):
    from biocognia.errors import OfflineRefused, ServerError

    for error, status, code in [
        (ServerError("Describe it in at least 10 characters.", code="invalid_request"), 400, "invalid_request"),
        (ServerError("Too many requests; try again later.", code="rate_limited"), 429, "rate_limited"),
        (ServerError("Could not reach the BioCognia platform.", code="unreachable"), 502, "unreachable"),
        (ServerError("bad gateway", code="bad_response"), 502, "unreachable"),
        (OfflineRefused("BIOCOGNIA_OFFLINE is set"), 502, "unreachable"),
    ]:
        monkeypatch.setattr(LICENSING, "feedback", lambda report, error=error: (_ for _ in ()).throw(error),
                            raising=False)
        reply = _submit(client, REPORT)
        assert (reply.status_code, reply.get_json()["code"]) == (status, code), error


def test_an_older_biocognia_client_has_the_page_send_it(client, monkeypatch):
    monkeypatch.setattr(LICENSING, "feedback", None, raising=False)
    reply = _submit(client, REPORT)
    assert reply.status_code == 501 and reply.get_json()["code"] == "client_too_old"
    assert client.get("/feedback/context").get_json()["relay"] is False


def test_too_many_or_too_big_files_are_refused_before_anything_is_sent(client, platform):
    four = [(f"s{i}.png", PNG) for i in range(4)]
    assert _submit(client, REPORT, files=four).status_code == 400
    big = [("big.png", PNG + b"\0" * (5 * 1024 * 1024))]
    assert _submit(client, REPORT, files=big).status_code == 400
    assert client.post("/feedback/submit", data={"report": "{not json"}).status_code == 400
    assert platform["reports"] == []


def test_the_relay_answers_this_machine_only(client, platform):
    reply = _submit(client, REPORT, environ_base={"REMOTE_ADDR": "10.0.0.8"})
    assert reply.status_code == 403
    assert platform["reports"] == []


def test_on_the_website_the_page_sends_it(client, platform, monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    reply = _submit(client, REPORT)
    assert reply.status_code == 409 and reply.get_json()["code"] == "website_page"
    assert client.get("/feedback/context").get_json()["relay"] is False


def test_the_email_is_never_logged(client, platform, caplog):
    caplog.set_level(logging.DEBUG)
    _submit(client, REPORT, files=[("shot.png", PNG)])
    assert "pat@lab.example.org" not in caplog.text


def test_the_routes_are_pythons(client):
    endpoints = {rule.endpoint for rule in plexora.app.url_map.iter_rules() if rule.rule.startswith("/feedback/")}
    assert endpoints == {"feedback.feedback_context", "feedback.feedback_submit"}
    for endpoint in endpoints:
        assert browser_routes.classify(endpoint) == "python"


def test_the_relay_through_the_biocognia_client(client, license_service, pro_license):
    if not hasattr(LICENSING, "feedback"):
        pytest.skip("Licensing.feedback arrives after biocognia 0.1.2")
    reply = _submit(client, {**REPORT, "email": None}, files=[("shot.png", PNG)])
    assert reply.status_code == 201, reply.get_json()
    sent = license_service.of("/v1/feedback")[0]["body"]
    assert sent["product"] == "plexora" and sent["certificate"].startswith("BIOC1.") and "email" not in sent
    assert license_service.of("/v1/feedback/fb_0123456789abcdef/attachments")[0]["raw"] == PNG


def test_the_navbar_has_the_feedback_icon_and_help_opens_the_form(client):
    page = client.get("/").get_data(as_text=True)
    assert 'id="feedback_button"' in page
    assert page.index('id="feedback_button"') < page.index('id="remote_globe"')
    for script in ("services/feedbackDiagnostics.js", "views/feedbackDialog.js", "services/feedbackMenu.js"):
        assert script in page
    assert page.index("views/feedbackDialog.js") < page.index("views/helpMenu.js")
    assert 'id="nav_report_issue"' in page and "Report a Bug&hellip;" in page
    assert 'id="nav_request_feature"' in page and "Request a Feature&hellip;" in page
    help_menu = (plexora.app.root_path + "/client/src/js/views/helpMenu.js")
    with open(help_menu, encoding="utf-8") as handle:
        source = handle.read()
    assert "github.com" not in source and 'PlexoraFeedback?.open("bug")' in source
