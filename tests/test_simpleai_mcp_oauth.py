"""OAuth 2.1 MCP: discovery, DCR, explicit consent, PKCE, rotating refresh.

Never mocks the PKCE verifier/check. Uses the real FastAPI app and isolated
SQLite in the project's existing MCP fixture.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from test_simpleai_mcp import ADMIN, ACCEPT, client, rpc, token
import main_impl as core

ISSUER = "https://aiproxy.cekluka.com"
RESOURCE = ISSUER + "/mcp"
CALLBACK = "https://example.test/oauth/callback"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def register(client, redirect=CALLBACK):
    response = client.post("/oauth/register", json={
        "client_name": "Test MCP Client",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code","refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def authorize(client, client_id, scope="simpleai:read offline_access",
              redirect=CALLBACK, state="session-state-12345", permission="read"):
    params={
        "response_type":"code","client_id":client_id,
        "redirect_uri":redirect,"scope":scope,"state":state,
        "code_challenge":CHALLENGE,"code_challenge_method":"S256",
        "resource":RESOURCE,
    }
    page=client.get("/oauth/authorize", params=params, auth=ADMIN)
    assert page.status_code==200,page.text
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"',page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"',page.text)
    assert attempt and csrf
    data={
        "authorization_request_id":html.unescape(attempt.group(1)),
        "csrf":html.unescape(csrf.group(1)),
        "permission":permission,"decision":"approve",
    }
    accepted=client.post("/oauth/authorize",data=data,auth=ADMIN,
                         headers={"Origin":ISSUER}, follow_redirects=False)
    assert accepted.status_code==303,accepted.text
    callback=accepted.headers["location"]
    assert callback.startswith(redirect+"?")
    parsed=parse_qs(urlsplit(callback).query)
    assert parsed["state"]==[state]
    assert parsed["iss"]==[ISSUER]
    return parsed["code"][0],data


def exchange(client, client_id, code, verifier=VERIFIER, redirect=CALLBACK,
             resource=RESOURCE):
    return client.post("/oauth/token", data={
        "grant_type":"authorization_code", "client_id":client_id, "code":code,
        "code_verifier":verifier,"redirect_uri":redirect,"resource":resource,
    })


def test_discovery_and_oauth_challenge(client):
    resource_meta=client.get("/.well-known/oauth-protected-resource")
    assert resource_meta.status_code==200
    assert resource_meta.json()["authorization_servers"]==[ISSUER]
    assert resource_meta.json()["resource"]==RESOURCE
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code==200
    metadata=client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code==200
    d=metadata.json()
    assert d["issuer"]==ISSUER
    assert d["simpleai_consent_revision"]=="idempotent-v2"
    assert d["authorization_response_iss_parameter_supported"] is True
    assert d["token_endpoint"]==ISSUER+"/oauth/token"
    assert d["registration_endpoint"]==ISSUER+"/oauth/register"
    assert d["code_challenge_methods_supported"]==["S256"]
    assert d["token_endpoint_auth_methods_supported"]==["none"]
    assert "offline_access" in d["scopes_supported"]
    missing=client.post("/mcp",headers={"Accept":ACCEPT},
        json={"jsonrpc":"2.0","id":1,"method":"initialize"})
    assert missing.status_code==401
    challenge=missing.headers["www-authenticate"]
    assert 'resource_metadata="' + ISSUER + '/.well-known/oauth-protected-resource"' in challenge
    assert 'error="invalid_token"' in challenge


@pytest.mark.parametrize("redirect",[
    "http://evil.example/callback",
    "https://example.test/callback#token",
    "https://user:password@example.test/cb",
    "https://example.test/cb\\evil",
    "file:///tmp/callback",
])
def test_dcr_rejects_insecure_redirects(client,redirect):
    response=client.post("/oauth/register",json={
        "client_name":"Unsafe",
        "redirect_uris":[redirect],
        "grant_types":["authorization_code"],
        "response_types":["code"],
    })
    assert response.status_code==400


def test_dcr_supports_localhost_native_clients(client):
    cid=register(client,"http://127.0.0.1:54321/callback")
    assert cid.startswith("simpleai_client_")


def test_oauth_code_pkce_and_one_time_use(client):
    cid=register(client)
    code, consent=authorize(client,cid)
    wrong=exchange(client,cid,code,verifier="b"*64)
    assert wrong.status_code==400
    assert wrong.json()["error"]=="invalid_grant"
    wrong_resource=exchange(client,cid,code,resource=ISSUER+"/other")
    assert wrong_resource.status_code==400
    ok=exchange(client,cid,code)
    assert ok.status_code==200,ok.text
    credentials=ok.json()
    assert credentials["access_token"].startswith("simpleai_oauth_")
    assert credentials["refresh_token"].startswith("simpleai_refresh_")
    assert credentials["expires_in"]==3600
    replay=exchange(client,cid,code)
    assert replay.status_code==400 and replay.json()["error"]=="invalid_grant"
    assert client.get("/oauth/authorize").status_code==401
    with core.get_db_connection() as db:
        assert db.execute("SELECT count(*) FROM MCPOAuthCodes WHERE consumed=1").fetchone()[0]>=1
        assert credentials["access_token"] not in json.dumps([
            dict(x) for x in db.execute("SELECT * FROM MCPOAuthAccess").fetchall()])
    ping=rpc(client,credentials["access_token"],"ping")
    assert ping.status_code==200
    assert ping.json()["result"]=={}


def test_initial_oauth_challenge_offers_operate_with_explicit_user_consent(client):
    """MCP clients use challenge scope before resource metadata; avoid read-only trap."""
    required = client.post("/mcp", headers={"Accept": ACCEPT}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
    })
    assert required.status_code == 401
    challenge = required.headers["www-authenticate"]
    assert 'scope="simpleai:read simpleai:operate offline_access"' in challenge

    # Reproduce how a real MCP client requests the scopes from that header.
    cid = register(client)
    code, _ = authorize(client, cid, scope="simpleai:read simpleai:operate",
                        permission="operate")
    granted = exchange(client, cid, code)
    assert granted.status_code == 200
    access = granted.json()
    assert set(access["scope"].split()) == {"simpleai:read", "simpleai:operate"}
    tools = rpc(client, access["access_token"], "tools/list").json()["result"]["tools"]
    assert "set_model_policy" in {entry["name"] for entry in tools}
    # It must NOT silently change policy without the per-action confirmation.
    denied = rpc(client, access["access_token"], "tools/call", {
        "name": "set_model_policy", "arguments": {"models": []},
    }).json()
    assert denied["result"]["isError"] is True

    # Read-only remains selectable even after both scopes were requested.
    read_code, _ = authorize(client, cid, scope="simpleai:read simpleai:operate",
                             permission="read")
    limited = exchange(client, cid, read_code).json()
    assert "simpleai:operate" not in limited["scope"].split()
    refused = rpc(client, limited["access_token"], "tools/call", {
        "name": "set_model_policy", "arguments": {"models": [], "confirm": True},
    }).json()
    assert refused["result"]["isError"] is True
    assert "mcp/www_authenticate" in refused["result"]["_meta"]


def test_initial_challenge_supports_refreshable_authorization_without_auto_operate(client):
    cid = register(client)
    code, _ = authorize(client, cid,
                        scope="simpleai:read simpleai:operate offline_access",
                        permission="read")
    result = exchange(client, cid, code)
    assert result.status_code == 200
    granted = result.json()
    assert "simpleai:operate" not in granted["scope"]
    assert granted["refresh_token"].startswith("simpleai_refresh_")
    refreshed = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "refresh_token": granted["refresh_token"],
    })
    assert refreshed.status_code == 200
    assert "simpleai:operate" not in refreshed.json()["scope"]


def test_scope_consent_and_upgrade_challenge(client):
    cid=register(client)
    code,_=authorize(client,cid,scope="simpleai:read simpleai:operate offline_access",
                     permission="read")
    credentials=exchange(client,cid,code).json()
    assert "simpleai:operate" not in credentials["scope"]
    tools=rpc(client,credentials["access_token"],"tools/list").json()["result"]["tools"]
    manage=next(t for t in tools if t["name"]=="set_model_policy")
    assert manage["securitySchemes"]==[{"type":"oauth2","scopes":["simpleai:operate"]}]
    result=rpc(client,credentials["access_token"],"tools/call",{
        "name":"set_model_policy","arguments":{"models":[],"confirm":True}})
    content=result.json()["result"]
    assert content["isError"] is True
    assert "mcp/www_authenticate" in content["_meta"]
    code2,_=authorize(client,cid,scope="simpleai:read simpleai:operate offline_access",
                      permission="operate")
    granted=exchange(client,cid,code2).json()
    assert "simpleai:operate" in granted["scope"]
    assert rpc(client,granted["access_token"],"tools/list").status_code==200


def test_refresh_rotates_and_replay_fails(client):
    cid=register(client)
    code,_=authorize(client,cid)
    original=exchange(client,cid,code).json()
    refresh=client.post("/oauth/token",data={
        "grant_type":"refresh_token","client_id":cid,
        "resource":RESOURCE,"refresh_token":original["refresh_token"]})
    assert refresh.status_code==200,refresh.text
    rotated=refresh.json()
    assert rotated["refresh_token"]!=original["refresh_token"]
    assert rotated["access_token"]!=original["access_token"]
    repeat=client.post("/oauth/token",data={
        "grant_type":"refresh_token","client_id":cid,
        "resource":RESOURCE,"refresh_token":original["refresh_token"]})
    assert repeat.status_code==400
    assert repeat.json()["error"]=="invalid_grant"
    assert rpc(client,rotated["access_token"],"ping").status_code==200



def test_chatgpt_token_exchange_can_omit_optional_resource_and_remains_bound_to_mcp(client):
    """Some OAuth clients omit resource on POST /oauth/token after the redirect."""
    cid = register(client)
    code, _ = authorize(client, cid, permission="read", scope="simpleai:read")
    exchanged = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": cid, "code": code,
        "code_verifier": VERIFIER, "redirect_uri": CALLBACK,
        # No resource here; the already-approved code is MCP-audience bound.
    })
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["access_token"]
    assert rpc(client, token, "ping").status_code == 200
    with core.get_db_connection() as db:
        audience = db.execute(
            "SELECT audience FROM MCPOAuthAccess WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
            (cid,)).fetchone()[0]
    assert audience == RESOURCE
    again = exchange(client, cid, code)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_wrong_resource_still_rejected_before_code_is_consumed(client):
    cid = register(client)
    code, _ = authorize(client, cid)
    bad = exchange(client, cid, code, resource="https://attacker.example/mcp")
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_request"
    good = exchange(client, cid, code)
    assert good.status_code == 200


def test_oauth_audit_uses_only_fixed_reason_codes_and_never_codes(client, monkeypatch):
    import simpleai_mcp_oauth as oauth
    events = []
    monkeypatch.setattr(oauth, "_audit_failure", lambda category: events.append(category))
    cid = register(client)
    code, _ = authorize(client, cid)
    incorrect = exchange(client, cid, code, verifier="b" * 64)
    assert incorrect.status_code == 400
    assert "authorization_code_pkce_mismatch" in events
    assert code not in json.dumps(events)
    assert VERIFIER not in json.dumps(events)
    good = exchange(client, cid, code)
    assert good.status_code == 200
    replay = exchange(client, cid, code)
    assert replay.status_code == 400
    assert "authorization_code_reused" in events
    assert code not in json.dumps(events)


def test_repeated_browser_consent_shows_actionable_error_without_new_code(client):
    cid = register(client)
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": CALLBACK,
        "scope": "simpleai:read", "state": "oauth-consent-state-reuse",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256",
        "resource": RESOURCE}, auth=ADMIN)
    assert page.status_code == 200
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"', page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert attempt and csrf
    fields = {"authorization_request_id": attempt.group(1),
              "csrf": csrf.group(1), "permission": "read", "decision": "approve"}
    first = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                        headers={"Origin": ISSUER}, follow_redirects=False)
    assert first.status_code == 303
    second = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                         headers={"Origin": ISSUER}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    # The same browser form does not mint a second grant, alter scopes, or
    # bypass the one-use authorization code's PKCE verifier.
    issued_code = parse_qs(urlsplit(first.headers["location"]).query)["code"][0]
    good = exchange(client, cid, issued_code)
    assert good.status_code == 200
    assert exchange(client, cid, issued_code).status_code == 400
    third = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                        headers={"Origin": ISSUER}, follow_redirects=False)
    assert third.status_code == 200
    assert "already been completed" in third.text
    assert "location" not in third.headers


def test_no_offline_access_means_no_refresh_token(client):
    cid=register(client)
    code,_=authorize(client,cid,scope="simpleai:read")
    issued=exchange(client,cid,code).json()
    assert "refresh_token" not in issued


def test_oauth_revocation_and_expiration(client):
    cid=register(client)
    code,_=authorize(client,cid)
    creds=exchange(client,cid,code).json()
    assert client.post("/oauth/revoke",data={
        "client_id":cid,"token":creds["access_token"]}).status_code==200
    assert rpc(client,creds["access_token"],"ping").status_code==401
    with core.get_db_connection() as db:
        db.execute("UPDATE MCPOAuthAccess SET expires_at=? WHERE client_id=?",
                   (int(time.time())-1,cid))
    assert rpc(client,creds["access_token"],"ping").status_code==401


def test_oauth_csrf_and_consumed_consent(client):
    cid=register(client)
    original_code,payload=authorize(client,cid)
    reused=client.post("/oauth/authorize",data=payload,auth=ADMIN,
                       headers={"Origin":ISSUER},follow_redirects=False)
    # A repeat before the code is redeemed must return the original callback.
    assert reused.status_code==303
    returned=parse_qs(urlsplit(reused.headers["location"]).query)["code"][0]
    assert returned == original_code
    assert exchange(client,cid,original_code).status_code==200
    # Once redeemed, the same form no longer redirects to a spent code.
    repeated=client.post("/oauth/authorize",data=payload,auth=ADMIN,
                         headers={"Origin":ISSUER},follow_redirects=False)
    assert repeated.status_code==200
    assert "already been completed" in repeated.text
    assert "location" not in repeated.headers
    code,second=authorize(client,cid)
    malformed={**second,"csrf":"c"*43}
    invalid=client.post("/oauth/authorize",data=malformed,auth=ADMIN,
                        headers={"Origin":ISSUER},follow_redirects=False)
    assert invalid.status_code==400
    hostile=client.post("/oauth/authorize",data=second,auth=ADMIN,
                        headers={"Origin":"https://evil.example"},follow_redirects=False)
    assert hostile.status_code==403


def test_auth_rejects_unregistered_callback_and_wrong_code_challenge(client):
    cid=register(client)
    params={
        "response_type":"code","client_id":cid,
        "redirect_uri":"https://evil.example/callback",
        "scope":"simpleai:operate","state":"abcd1234efgh5678",
        "code_challenge":CHALLENGE,"code_challenge_method":"S256",
        "resource":RESOURCE}
    wrong=client.get("/oauth/authorize",params=params,auth=ADMIN,follow_redirects=False)
    assert wrong.status_code==400
    assert "location" not in wrong.headers
    params["redirect_uri"]=CALLBACK
    params["code_challenge_method"]="plain"
    assert client.get("/oauth/authorize",params=params,auth=ADMIN).status_code==400


def test_oauth_does_not_authenticate_regular_api_or_admin(client):
    cid=register(client)
    code,_=authorize(client,cid)
    access=exchange(client,cid,code).json()["access_token"]
    assert client.get("/admin/keys",headers={
        "Authorization":"Bearer "+access}).status_code==401
    assert client.get("/v1/models",headers={
        "Authorization":"Bearer "+access}).status_code==401


def test_admin_can_list_and_revoke_all_oauth_connection_tokens(client):
    cid=register(client)
    code,_=authorize(client,cid)
    issued=exchange(client,cid,code).json()
    listing=client.get("/admin/api/mcp/oauth/connections",auth=ADMIN)
    assert listing.status_code==200
    assert cid in listing.text
    assert issued["access_token"] not in listing.text
    assert issued["refresh_token"] not in listing.text
    assert client.get("/admin/api/mcp/oauth/connections").status_code==401
    bad=client.post("/admin/api/mcp/oauth/connections/"+cid+"/revoke",auth=ADMIN)
    assert bad.status_code==403
    good=client.post("/admin/api/mcp/oauth/connections/"+cid+"/revoke",auth=ADMIN,
                     headers={"Origin":ISSUER,"X-SimpleAI-Admin-Action":"1"})
    assert good.status_code==200,good.text
    assert rpc(client,issued["access_token"],"ping").status_code==401
    refresh=client.post("/oauth/token",data={
        "grant_type":"refresh_token","client_id":cid,
        "refresh_token":issued["refresh_token"]})
    assert refresh.status_code==400


def test_refresh_can_omit_optional_resource_but_not_substitute_another(client):
    cid=register(client)
    code,_=authorize(client,cid)
    issued=exchange(client,cid,code).json()
    invalid=client.post("/oauth/token",data={
        "grant_type":"refresh_token","client_id":cid,
        "resource":"https://evil.example/mcp","refresh_token":issued["refresh_token"]})
    assert invalid.status_code==400
    ok=client.post("/oauth/token",data={
        "grant_type":"refresh_token","client_id":cid,
        "refresh_token":issued["refresh_token"]})
    assert ok.status_code==200,ok.text


def _consent_form(client, client_id):
    """Open a real OAuth consent page and extract its one-time form tokens."""
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": CALLBACK, "scope": "simpleai:read",
        "state": "mobile-login-state-1234", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
    }, auth=ADMIN)
    assert page.status_code == 200
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"', page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert attempt and csrf
    return {"authorization_request_id": attempt.group(1),
            "csrf": csrf.group(1), "permission": "read", "decision": "approve"}


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "null"},
    {"Referer": ISSUER + "/oauth/authorize?client_id=safe"},
    {"Origin": ISSUER},
])
def test_mobile_privacy_browser_can_authorize_with_valid_csrf(client, headers):
    """Brave may suppress Referer and Origin; PKCE consent remains valid."""
    cid = register(client)
    data = _consent_form(client, cid)
    response = client.post("/oauth/authorize", data=data, auth=ADMIN,
                           headers=headers, follow_redirects=False)
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["mobile-login-state-1234"]
    assert query["iss"] == [ISSUER]
    code = query["code"][0]
    exchanged = exchange(client, cid, code)
    assert exchanged.status_code == 200, exchanged.text


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "null"},
    {"Origin": ISSUER},
])
def test_mobile_consent_missing_or_wrong_csrf_still_blocked(client, headers):
    cid = register(client)
    valid = _consent_form(client, cid)
    for altered in ({**valid, "csrf": "x" * 43},
                    {k: v for k, v in valid.items() if k != "csrf"}):
        denied = client.post("/oauth/authorize", data=altered, auth=ADMIN,
                             headers=headers, follow_redirects=False)
        assert denied.status_code == 400, denied.text
        assert "location" not in denied.headers
    allowed = client.post("/oauth/authorize", data=valid, auth=ADMIN,
                          headers=headers, follow_redirects=False)
    assert allowed.status_code == 303, allowed.text


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Referer": "https://evil.example/oauth/authorize"},
    {"Referer": "https://aiproxy.cekluka.com.evil.example/oauth/authorize"},
    {"Origin": "null", "Referer": "https://evil.example/oauth/authorize"},
])
def test_mobile_consent_explicitly_foreign_origin_is_rejected(client, headers):
    cid = register(client)
    data = _consent_form(client, cid)
    denied = client.post("/oauth/authorize", data=data, auth=ADMIN,
                         headers=headers, follow_redirects=False)
    assert denied.status_code == 403, denied.text
    assert "location" not in denied.headers
    # Rejection must not consume a valid pending authorization.
    allowed = client.post("/oauth/authorize", data=data, auth=ADMIN,
                          follow_redirects=False)
    assert allowed.status_code == 303


def test_admin_oauth_diagnostics_are_auth_protected_and_never_show_codes(client, monkeypatch):
    import simpleai_mcp_logging as logs
    recorded = []
    original = logs.record

    def capture(component, level, code, *, exception_type=""):
        recorded.append((component, level, code, exception_type))
        return original(component, level, code, exception_type=exception_type)

    monkeypatch.setattr(logs, "record", capture)
    cid = register(client)
    code, _ = authorize(client, cid)
    failure = exchange(client, cid, code, verifier="b" * 64)
    assert failure.status_code == 400
    assert ("mcp_oauth", "warning", "authorization_code_pkce_mismatch", "") in recorded

    denied = client.get("/admin/api/mcp/oauth/diagnostics")
    assert denied.status_code == 401
    result = client.get("/admin/api/mcp/oauth/diagnostics", auth=ADMIN)
    assert result.status_code == 200
    body = result.json()
    assert "authorization_code_pkce_mismatch" in json.dumps(body)
    assert code not in json.dumps(body)
    assert VERIFIER not in json.dumps(body)
    assert body["note"].startswith("Privacy-safe")


def test_consent_duplicate_cannot_escalate_read_scope_to_operate(client):
    cid = register(client)
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": CALLBACK,
        "scope": "simpleai:read simpleai:operate",
        "state": "same-form-two-submits", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
    }, auth=ADMIN)
    assert page.status_code == 200
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"', page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert attempt and csrf
    fields = {
        "authorization_request_id": attempt.group(1), "csrf": csrf.group(1),
        "permission": "read", "decision": "approve",
    }
    first = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                        follow_redirects=False)
    assert first.status_code == 303
    # A duplicate POST tries to ask for operate. It must not issue another
    # grant, nor change the original read-only authorization scope.
    escalated = client.post("/oauth/authorize",
                            data={**fields, "permission": "operate"}, auth=ADMIN,
                            follow_redirects=False)
    assert escalated.status_code == 303
    assert escalated.headers["location"] == first.headers["location"]
    code = parse_qs(urlsplit(first.headers["location"]).query)["code"][0]
    issued = exchange(client, cid, code)
    assert issued.status_code == 200
    assert "simpleai:operate" not in issued.json()["scope"].split()


def test_duplicate_consent_with_wrong_csrf_never_replays_authorization_code(client):
    cid = register(client)
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": CALLBACK,
        "scope": "simpleai:read",
        "state": "same-form-csrf-replay", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
    }, auth=ADMIN)
    assert page.status_code == 200
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"', page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    fields = {
        "authorization_request_id": attempt.group(1), "csrf": csrf.group(1),
        "permission": "read", "decision": "approve",
    }
    first = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                        follow_redirects=False)
    assert first.status_code == 303
    invalid = client.post("/oauth/authorize",
                          data={**fields, "csrf": "x" * 43}, auth=ADMIN,
                          follow_redirects=False)
    assert invalid.status_code == 400
    assert "location" not in invalid.headers
    retried = client.post("/oauth/authorize", data=fields, auth=ADMIN,
                          follow_redirects=False)
    assert retried.status_code == 303
    assert retried.headers["location"] == first.headers["location"]


def test_consent_form_stays_same_origin_without_javascript(client):
    cid = register(client)
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid,
        "redirect_uri": CALLBACK, "scope": "simpleai:read",
        "state": "safe-browser-form-123", "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
    }, auth=ADMIN)
    assert page.status_code == 200
    assert 'id="mcp-consent-form"' in page.text
    # No absolute action to another hostname. CSP 'form-action self' must be
    # compatible even if a mobile in-app browser opened an alias URL.
    assert 'action="/oauth/authorize"' in page.text
    assert 'name="csrf"' in page.text
    assert '<script' not in page.text
    assert 'Connecting…' not in page.text
    assert "form-action 'self'" in page.headers["content-security-policy"]


def test_noncanonical_mobile_consent_host_redirects_to_known_issuer(client):
    """The user screenshot shows proxy.cekluka.com (missing the 'ai' prefix).

    Consent must redirect to the canonical OAuth issuer *before* Basic login,
    and must preserve the original OAuth parameters without leaking them to a
    client-supplied Host. After redirect, the POST action remains same-origin.
    """
    cid = register(client)
    params = {
        "response_type": "code", "client_id": cid,
        "redirect_uri": CALLBACK,
        "scope": "simpleai:read", "state": "mobile-origin-state-1234",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256", "resource": RESOURCE,
    }
    wrong_host = client.get("/oauth/authorize", params=params,
                            headers={"Host": "proxy.cekluka.com"},
                            follow_redirects=False)
    assert wrong_host.status_code == 302, wrong_host.text
    location = wrong_host.headers["location"]
    assert location.startswith(ISSUER + "/oauth/authorize?")
    assert "proxy.cekluka.com/oauth/authorize" not in location
    assert parse_qs(urlsplit(location).query)["state"] == [
        "mobile-origin-state-1234"]
    assert wrong_host.headers["cache-control"] == "no-store"

    # A browser following the new issuer can authenticate and use the
    # ordinary relative POST without being blocked by form-action 'self'.
    page = client.get("/oauth/authorize", params=params, auth=ADMIN,
                      headers={"Host": "aiproxy.cekluka.com"})
    assert page.status_code == 200, page.text
    assert 'action="/oauth/authorize"' in page.text
    assert 'name="csrf"' in page.text


def test_consent_form_arrival_and_redirect_emits_only_safe_event_codes(client, monkeypatch):
    import simpleai_mcp_logging as logs
    events = []
    original = logs.record

    def capture(component, level, code, *, exception_type=""):
        events.append((component, level, code, exception_type))
        return original(component, level, code, exception_type=exception_type)

    monkeypatch.setattr(logs, "record", capture)
    cid = register(client)
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": CALLBACK,
        "scope": "simpleai:read", "state": "test-consent-post-observability",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256",
        "resource": RESOURCE,
    }, auth=ADMIN)
    assert page.status_code == 200
    attempt = re.search(r'name="authorization_request_id" value="([^"]+)"', page.text)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert attempt and csrf
    response = client.post("/oauth/authorize", data={
        "authorization_request_id": attempt.group(1), "csrf": csrf.group(1),
        "permission": "read", "decision": "approve",
    }, auth=ADMIN, follow_redirects=False)
    assert response.status_code == 303
    assert ("mcp_oauth", "info", "consent_post_received", "") in events
    assert ("mcp_oauth", "info", "consent_redirect_issued", "") in events
    assert not any(attempt.group(1) in str(event) or csrf.group(1) in str(event)
                   for event in events)
