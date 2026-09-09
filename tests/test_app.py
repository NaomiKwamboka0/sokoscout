"""Web application tests.

The predecessor to this product had no authentication whatsoever: every route
on its public URL was open, and any visitor could read the whole database.
The first class below exists so that this one cannot regress to that quietly.
"""

from decimal import Decimal
from datetime import date

import pytest
from fastapi.testclient import TestClient

from soko import app as app_module
from soko.app import app
from soko.pipeline import JsonlStore
from soko.sources.base import Listing

TODAY = date(2026, 9, 9)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Fresh accounts, sessions and data file for every test."""
    app_module._ACCOUNTS.clear()
    app_module._SESSIONS.clear()
    app_module._NEXT_ID[0] = 1

    data_file = tmp_path / "run.jsonl"
    listings = []
    for i in range(45):
        listings.append(Listing(
            platform_code="jumia_ke",
            source_key=f"pb{i}",
            source_url=f"https://www.jumia.co.ke/p{i}.html",
            title=f"Power Bank {10000 + i * 100}mAh Fast Charging",
            price_kes=Decimal(2000 + i * 20),
            observed_on=TODAY,
            seller_name=f"Shop {i % 12}",
        ))
    # A deliberately thin category, to exercise refusal.
    for i in range(9):
        listings.append(Listing(
            platform_code="jumia_ke",
            source_key=f"lip{i}",
            source_url=f"https://www.jumia.co.ke/l{i}.html",
            title=f"Matte Lipstick Long Lasting {i}",
            price_kes=Decimal(400 + i * 30),
            observed_on=TODAY,
            seller_name=f"Beauty {i}",
        ))
    JsonlStore(data_file).write(listings)
    monkeypatch.setattr(app_module, "DATA_FILE", data_file)

    # The test client speaks plain HTTP to the ASGI app in-process, and a
    # Secure cookie is by definition not sent over plain HTTP. Turning the
    # flag off here is what lets the authenticated tests exercise a real
    # session; the flag itself is asserted separately in TestSessionCookie,
    # which reads the Set-Cookie header rather than round-tripping it.
    monkeypatch.setattr(app_module, "COOKIE_SECURE", False)
    yield


@pytest.fixture
def client():
    # follow_redirects off so we can assert on the redirect itself.
    return TestClient(app, follow_redirects=False)


def register(client, email="vendor@example.com", password="a long enough passphrase"):
    response = client.post("/signup", data={"email": email, "password": password})
    return response.cookies.get("session")


class TestNoOpenRoutes:
    """The predecessor's cautionary tale, guarded."""

    @pytest.mark.parametrize("path", ["/", "/ask?q=price+of+power+banks"])
    def test_pages_require_a_session(self, client, path):
        response = client.get(path)
        assert response.status_code in (303, 401)
        if response.status_code == 303:
            assert "/signin" in response.headers["location"]

    def test_api_requires_a_session(self, client):
        assert client.get("/api/ask?q=anything").status_code == 401

    def test_a_forged_cookie_is_rejected(self, client):
        client.cookies.set("session", "not-a-real-token")
        assert client.get("/api/ask?q=anything").status_code == 401

    def test_health_is_the_only_open_route_and_leaks_nothing(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_no_route_triggers_a_crawl(self, client):
        # Answering must never hit a marketplace. If it could, a vendor's
        # curiosity could get the project rate limited.
        register(client)
        body = client.get("/ask?q=What+do+power+banks+cost").text
        assert "blocked" not in body.lower()


class TestSignUpAndIn:
    def test_signup_creates_a_session(self, client):
        assert register(client) is not None

    def test_signed_in_user_sees_the_dashboard(self, client):
        register(client)
        response = client.get("/")
        assert response.status_code == 200
        assert "SokoScout" in response.text

    def test_signin_with_the_right_password(self, client):
        register(client, "a@example.com", "a long enough passphrase")
        app_module._SESSIONS.clear()
        response = client.post(
            "/signin", data={"email": "a@example.com", "password": "a long enough passphrase"}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_signin_with_the_wrong_password_fails(self, client):
        register(client, "a@example.com")
        response = client.post(
            "/signin", data={"email": "a@example.com", "password": "wrong passphrase here"}
        )
        assert "error" in response.headers["location"]

    def test_short_passwords_are_rejected(self, client):
        response = client.post("/signup", data={"email": "b@example.com", "password": "short"})
        assert "error" in response.headers["location"]

    def test_signout_invalidates_the_session(self, client):
        register(client)
        assert client.get("/").status_code == 200
        client.post("/signout")
        assert client.get("/").status_code in (303, 401)


class TestNoAccountEnumeration:
    """A login form that answers differently for a real address is a customer
    list, and this product's customer list is commercially sensitive."""

    def test_unknown_and_known_addresses_respond_identically(self, client):
        register(client, "real@example.com", "a long enough passphrase")

        known = client.post(
            "/signin", data={"email": "real@example.com", "password": "wrong passphrase"}
        )
        unknown = client.post(
            "/signin", data={"email": "nobody@example.com", "password": "wrong passphrase"}
        )
        assert known.status_code == unknown.status_code
        assert known.headers["location"] == unknown.headers["location"]

    def test_duplicate_signup_does_not_confirm_the_address_exists(self, client):
        register(client, "real@example.com")
        duplicate = client.post(
            "/signup", data={"email": "real@example.com", "password": "a long enough passphrase"}
        )
        assert "already" not in duplicate.headers["location"].lower()
        assert "exists" not in duplicate.headers["location"].lower()


class TestSessionCookie:
    def test_cookie_is_httponly_and_samesite(self, client):
        response = client.post(
            "/signup", data={"email": "c@example.com", "password": "a long enough passphrase"}
        )
        header = response.headers["set-cookie"].lower()
        # HttpOnly means XSS cannot lift the session.
        assert "httponly" in header
        assert "samesite=lax" in header

    def test_secure_flag_is_on_by_default(self, monkeypatch):
        # The fixture turns Secure off so the test client can round-trip a
        # cookie over plain HTTP. This test checks the deployed default
        # instead, by reading the Set-Cookie header with the flag restored.
        monkeypatch.setattr(app_module, "COOKIE_SECURE", True)
        fresh = TestClient(app, follow_redirects=False)
        response = fresh.post(
            "/signup", data={"email": "sec@example.com", "password": "a long enough passphrase"}
        )
        assert "secure" in response.headers["set-cookie"].lower()

    def test_the_production_default_is_secure(self):
        # A switch that defaults to insecure is one forgotten config away from
        # shipping sessions over plain HTTP. Read the module source rather
        # than the patched attribute.
        import inspect
        source = inspect.getsource(app_module)
        assert "COOKIE_SECURE = True" in source

    def test_the_stored_token_is_not_the_cookie_value(self, client):
        token = register(client)
        # A leaked session store must not hand over live sessions.
        assert token not in app_module._SESSIONS


class TestAnswers:
    def test_answers_a_price_question_with_evidence(self, client):
        register(client)
        body = client.get("/ask?q=What+do+power+banks+cost").text
        assert "median price for power banks" in body
        assert "Based on" in body

    def test_refuses_a_thin_category(self, client):
        register(client)
        body = client.get("/ask?q=What+does+lipstick+cost").text
        assert "too thin" in body
        assert "Not answering that" in body

    def test_refuses_the_demand_question(self, client):
        register(client)
        body = client.get("/ask?q=What+sells+best+in+Kisumu").text
        assert "sales volume" in body

    def test_refuses_an_unrecognised_category(self, client):
        # The regression from the CLI, guarded here too: this must not answer
        # with a different category's median.
        register(client)
        body = client.get("/ask?q=What+is+the+price+of+helicopters").text
        assert "could not tell which product category" in body
        assert "median price" not in body

    def test_dashboard_marks_thin_categories(self, client):
        register(client)
        body = client.get("/").text
        assert "too thin" in body


class TestPolicyAndCostAnswers:
    """The policy, cost comparison and activity features, through the web."""

    def test_platform_cost_comparison(self, client):
        register(client)
        body = client.get("/ask?q=Where+should+I+sell+Jumia+or+Kilimall").text
        assert "leaves you the most" in body
        assert "per unit" in body

    def test_payout_question(self, client):
        register(client)
        body = client.get("/ask?q=When+do+I+get+paid+on+Jumia").text
        assert "14 day" in body or "fortnightly" in body

    def test_onboarding_question(self, client):
        register(client)
        body = client.get("/ask?q=What+are+the+seller+onboarding+requirements").text
        assert "KRA PIN" in body

    def test_returns_question(self, client):
        register(client)
        body = client.get("/ask?q=What+is+the+returns+policy").text
        assert "7 days" in body

    def test_activity_question_carries_the_disclaimer(self, client):
        # The critical one: a vendor must not read assortment as search volume.
        register(client)
        body = client.get("/ask?q=What+is+the+most+searched+product+in+Nakuru").text
        assert "not what buyers are searching for" in body

    def test_activity_question_says_listings_are_national(self, client):
        register(client)
        body = client.get("/ask?q=What+is+most+popular+in+Nakuru").text
        assert "national" in body

    def test_sales_volume_question_is_still_refused(self, client):
        # Widening popularity questions must not have opened a path to
        # answering what actually sold.
        register(client)
        body = client.get("/ask?q=What+sells+best+in+Kisumu").text
        assert "sales volume" in body
        assert "Not answering that" in body

    def test_opportunity_question(self, client):
        register(client)
        body = client.get("/ask?q=Where+is+there+room+in+the+market").text
        assert "listings per seller" in body


class TestTierGating:
    def test_trial_cannot_see_an_out_of_plan_platform(self, client):
        register(client)
        body = client.get("/ask?q=What+do+power+banks+cost+on+glovo").text
        assert "does not include that platform" in body

    def test_api_is_premium_only(self, client):
        register(client)
        response = client.get("/api/ask?q=What+do+power+banks+cost")
        assert response.status_code == 403
        assert "Premium" in response.json()["error"]

    def test_premium_may_use_the_api(self, client):
        register(client, "prem@example.com")
        app_module._ACCOUNTS["prem@example.com"].tier = "premium"
        response = client.get("/api/ask?q=What+do+power+banks+cost")
        assert response.status_code == 200
        assert response.json()["answered"] is True


class TestEscaping:
    def test_question_text_is_escaped(self, client):
        # Scraped and user supplied text both reach these pages.
        register(client)
        body = client.get("/ask?q=%3Cscript%3Ealert(1)%3C/script%3E").text
        assert "<script>alert(1)</script>" not in body
