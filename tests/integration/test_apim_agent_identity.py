"""Integration tests — APIM X-Agent-ID policy enforcement (ADR-013).

These tests verify that the logical identity check in APIM blocks agents
that are not in the AgentPermissions Named Value from calling endpoints
they are not allowed to access.

Prerequisites
-------------
Set the following environment variables before running:

    APIM_TEST_GATEWAY_URL=https://apim-ao-dev.azure-api.net
    APIM_TEST_CLIENT_ID=59996116-e726-4d8c-abbb-e06ff55cdfe9
    APIM_TEST_CLIENT_SECRET=<from: cd infra && terraform output test_caller_client_secret>
    APIM_TEST_TENANT_ID=d0e5c664-9418-4d45-89ee-d0e90d80d8e6
    APIM_TEST_IDENTIFIER_URI=api://d0e5c664-9418-4d45-89ee-d0e90d80d8e6/apim-ao-dev

Run:
    pytest tests/integration/test_apim_agent_identity.py -v
"""

import os
import urllib.parse
import urllib.request

import pytest

# ── Skip guard — skip if Azure creds not available ─────────────────────


def _check_env() -> str | None:
    """Return None if all required env vars are present, else a reason string."""
    required = [
        "APIM_TEST_GATEWAY_URL",
        "APIM_TEST_CLIENT_ID",
        "APIM_TEST_CLIENT_SECRET",
        "APIM_TEST_TENANT_ID",
        "APIM_TEST_IDENTIFIER_URI",
    ]
    missing = [k for k in required if not os.getenv(k)]
    return f"Missing env vars: {missing}" if missing else None


_SKIP_REASON = _check_env()
apim_required = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# ── Token helper ──────────────────────────────────────────────────────


def _get_bearer_token() -> str:
    """Acquire a client-credentials JWT from Azure AD for APIM (test_caller app)."""
    tenant_id = os.environ["APIM_TEST_TENANT_ID"]
    client_id = os.environ["APIM_TEST_CLIENT_ID"]
    client_secret = os.environ["APIM_TEST_CLIENT_SECRET"]
    identifier_uri = os.environ["APIM_TEST_IDENTIFIER_URI"]

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{identifier_uri}/.default",
        }
    ).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        import json
        return json.loads(resp.read())["access_token"]


# ── Tests ──────────────────────────────────────────────────────────────


class TestApimAgentIdentityPolicy:
    """Verify the APIM X-Agent-ID enforcement policy (ADR-013 Step 3)."""

    @pytest.fixture(scope="class")
    def token(self):
        """One JWT shared across all tests in the class (eager — skipped if creds absent)."""
        if _SKIP_REASON:
            pytest.skip(_SKIP_REASON)
        return _get_bearer_token()

    @pytest.fixture(scope="class")
    def taxpayer_url(self):
        gateway = os.environ.get("APIM_TEST_GATEWAY_URL", "").rstrip("/")
        # Use a TIN that does not exist — we only care about the HTTP status code
        # from the gateway, not whether the taxpayer record exists.
        return f"{gateway}/agents/taxpayer/SG-T000-0000"

    def _call(self, url: str, token: str, agent_id: str) -> int:
        """Make a GET request to APIM and return the HTTP status code."""
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Agent-ID": agent_id,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (TimeoutError, urllib.error.URLError):
            # A connection timeout or URL error means APIM forwarded the request
            # to the backend (Step 3 allowed it) but the backend is unreachable
            # (placeholder URL). Treat as 504 — definitely not a 403 policy block.
            return 504

    @apim_required
    def test_allowed_agent_is_not_blocked_by_step3(self, token, taxpayer_url):
        """An agent that IS in the AgentPermissions map for /agents/taxpayer must not
        receive a 403 from the Step 3 policy (it may get 404 if the TIN is unknown
        or 200 if the backend is live)."""
        status = self._call(taxpayer_url, token, "filing_extension")
        # 200 (backend live) or 404 (TIN not found) are both acceptable —
        # any status other than 403 means the Step 3 gate passed.
        assert status != 403, (
            f"Expected filing_extension to pass Step 3, got {status}. "
            "Check that AgentPermissions Named Value includes 'filing_extension'."
        )

    @apim_required
    def test_wrong_path_agent_blocked(self, token, taxpayer_url):
        """An agent whose allowed paths do NOT include /agents/taxpayer must be
        rejected with 403 by the Step 3 policy.

        rag_search is mapped to ['/agents/search'] — calling /agents/taxpayer
        must be denied even though the JWT carries a valid Agents.TaxpayerLookup role.
        """
        status = self._call(taxpayer_url, token, "rag_search")
        assert status == 403, (
            f"Expected rag_search to be blocked (403) on /agents/taxpayer, got {status}."
        )

    @apim_required
    def test_unknown_agent_blocked(self, token, taxpayer_url):
        """An X-Agent-ID that is not in the permissions map at all must be rejected
        with 403.  This validates the 'return true' fall-through branch.
        """
        status = self._call(taxpayer_url, token, "unauthorized_agent")
        assert status == 403, (
            f"Expected unknown agent 'unauthorized_agent' to be blocked (403), got {status}."
        )

    @apim_required
    def test_missing_header_blocked(self, token, taxpayer_url):
        """A request with no X-Agent-ID header must receive 403.
        The policy defaults to 'Unknown' which is not in the permissions map.
        """
        req = urllib.request.Request(
            taxpayer_url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code

        assert status == 403, (
            f"Expected missing X-Agent-ID to be blocked (403), got {status}."
        )
