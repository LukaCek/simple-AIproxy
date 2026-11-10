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
    _,payload=authorize(client,cid)
    reused=client.post("/oauth/authorize",data=payload,auth=ADMIN,
                       headers={"Origin":ISSUER},follow_redirects=False)
    assert reused.status_code==400
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
