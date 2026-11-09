"""MCP contract, authentication, separation of duties and credential-leak tests."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main_impl as core
import simpleai_mcp as mcp
import simpleai_mcp_logging as mcp_logging
import simpleai_mcp_tools as tools

ADMIN = ("admin", "admin")
ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def client(tmp_path, monkeypatch):
    mcp.install(core)
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "mcp.db")
    monkeypatch.setattr(core, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(core, "ADMIN_PASSWORD", "admin")
    core.init_database()
    with TestClient(core.app) as c:
        yield c


def token(client, scope="read"):
    response=client.post("/admin/api/mcp/tokens",auth=ADMIN,headers={
        "X-SimpleAI-Admin-Action":"1"},json={"name":"MCP Test", "scope":scope,"expires_days":7})
    assert response.status_code==200,response.text
    return response.json()


def rpc(client, key, method, params=None, rpc_id=1, **extra):
    body={"jsonrpc":"2.0","id":rpc_id,"method":method}
    if params is not None:
        body["params"]=params
    return client.post("/mcp",headers={
        "Authorization":"Bearer "+key, "Accept":ACCEPT, **extra},json=body)


def call(client, key, name, **arguments):
    return rpc(client,key,"tools/call",{"name":name,"arguments":arguments}).json()


def test_default_deny_and_mcp_version_negotiation(client):
    no=client.post("/mcp",headers={"Accept":ACCEPT},json={
        "jsonrpc":"2.0","id":1,"method":"initialize"})
    assert no.status_code==401
    assert "token" not in no.text.lower() or "required" in no.text.lower()
    obj=token(client)
    assert "token" in obj and obj["token"].startswith("simpleai_mcp_")
    assert "token_hash" not in obj
    init=rpc(client,obj["token"],"initialize",{
        "protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{
            "name":"pytest","version":"1"}})
    assert init.status_code==200,init.text
    assert init.json()["result"]["serverInfo"]["name"]=="simpleai-proxy-mcp"
    assert init.json()["result"]["protocolVersion"]=="2026-07-28"
    assert rpc(client,obj["token"],"ping").json()["result"]=={}
    assert client.get("/mcp").status_code==405


def test_token_is_hashed_never_returned_by_list(client):
    issued=token(client, "operate")
    with sqlite3.connect(str(core.DB_PATH)) as db:
        row=db.execute("SELECT token_hash FROM MCPAccessTokens WHERE id=?",
                       (issued["id"],)).fetchone()
        assert len(row[0])==64
        assert issued["token"] not in row[0]
    listing=client.get("/admin/api/mcp/tokens",auth=ADMIN)
    assert listing.status_code==200
    assert issued["id"] in listing.text
    assert issued["token"] not in listing.text
    assert "token_hash" not in listing.text
    assert client.get("/admin/api/mcp/tokens").status_code==401


def test_scopes_hide_write_tools_and_reject_invocation(client):
    read=token(client,"read")["token"]
    listed=rpc(client,read,"tools/list").json()["result"]["tools"]
    names={tool["name"] for tool in listed}
    assert {"system_status","list_workers","request_logs"}<=names
    assert "set_model_policy" not in names
    denied=rpc(client,read,"tools/call",{"name":"set_model_policy",
        "arguments":{"models":[],"confirm":True}})
    assert denied.json()["error"]["code"]==-32602
    operate=token(client,"operate")["token"]
    operations=rpc(client,operate,"tools/list").json()["result"]["tools"]
    assert "set_model_policy" in {tool["name"] for tool in operations}
    assert next(tool for tool in operations if tool["name"]=="run_playground")[
        "annotations"]["readOnlyHint"] is False


def test_revoke_blocks_existing_token(client):
    issued=token(client)
    assert rpc(client,issued["token"],"ping").status_code==200
    r=client.post("/admin/api/mcp/tokens/"+issued["id"]+"/revoke",
                  auth=ADMIN,headers={"X-SimpleAI-Admin-Action":"1"})
    assert r.status_code==200
    assert rpc(client,issued["token"],"ping").status_code==401
    assert client.post("/admin/api/mcp/tokens/"+issued["id"]+"/revoke",
                       auth=ADMIN).status_code==403


def test_expired_and_malformed_tokens_rejected(client):
    item=token(client)
    with sqlite3.connect(str(core.DB_PATH)) as db:
        db.execute("UPDATE MCPAccessTokens SET expires_at=? WHERE id=?",
                   (int(time.time())-1,item["id"]))
    assert rpc(client,item["token"],"ping").status_code==401
    assert rpc(client,"simpleai_mcp_INVALID","ping").status_code==401


def test_origin_and_content_type_guard(client):
    item=token(client)
    hostile=rpc(client,item["token"],"ping",origin="https://evil.example")
    assert hostile.status_code==403
    bad=client.post("/mcp",headers={
        "Authorization":"Bearer "+item["token"],"Accept":ACCEPT,
        "Content-Type":"text/plain"},content="hello")
    assert bad.status_code==415
    assert client.post("/mcp",headers={"Authorization":"Bearer "+item["token"],
        "Accept":"application/json"},json={"jsonrpc":"2.0","id":1,"method":"ping"}).status_code==406
    notification=client.post("/mcp",headers={"Authorization":"Bearer "+item["token"],
        "Accept":ACCEPT},json={"jsonrpc":"2.0","method":"notifications/initialized"})
    assert notification.status_code==202


def test_all_read_results_exclude_secrets_and_request_content(client,monkeypatch):
    monkeypatch.setattr(core,"config_data",{"providers":[{
        "name":"codex","api_mode":"codex_responses","models":["gpt-test"],
        "api_key":"PROVIDER_PRIVATE_KEY","access_token":"PRIVATE_OAUTH_ACCESS",
        "refresh_token":"PRIVATE_OAUTH_REFRESH","oauth":False,
        "url":"https://my-private-token@backend.example/v1"
    }],"groups":{},"model_profiles":{}})
    with core.get_db_connection() as db:
        db.execute("INSERT INTO Logs(api_key,api_key_name,request_id,requested_model,"
                   "status_code,prompt,output,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("PRIVATE_API_KEY","test","my-request","gpt-test",502,
                    "CONFIDENTIAL_USER_PROMPT","CONFIDENTIAL_MODEL_RESPONSE",
                    "Authorization: Bearer PRIVATE_API_KEY","now"))
        db.execute("INSERT INTO ProviderAttempts(request_id,attempt_no,provider_name,"
                   "provider_model,status_code,action,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   ("my-request",1,"codex","gpt-test",401,"retry",
                    "refresh_token=PRIVATE_OAUTH_REFRESH","now"))
    read=token(client)["token"]
    for name in ("system_status","list_groups","list_providers","list_models",
                 "request_logs","provider_attempts","error_overview","codex_status","ui_state"):
        response=call(client,read,name)
        assert response["result"]["isError"] is False,(name,response)
        text=json.dumps(response)
        for secret in ("PROVIDER_PRIVATE_KEY","PRIVATE_OAUTH_ACCESS",
                       "PRIVATE_OAUTH_REFRESH","PRIVATE_API_KEY",
                       "CONFIDENTIAL_USER_PROMPT","CONFIDENTIAL_MODEL_RESPONSE"):
            assert secret not in text,(name,secret)


def test_mcp_write_requires_explicit_confirmation(client):
    key=token(client,"operate")["token"]
    out=call(client,key,"set_model_policy",models=[])
    assert out["result"]["isError"] is True
    assert "Tool error" in out["result"]["content"][0]["text"]


def test_unauthorized_browser_admin_credentials_do_not_work_as_mcp_token(client):
    import base64
    credentials=base64.b64encode(b"admin:admin").decode()
    r=client.post("/mcp",headers={"Authorization":"Basic "+credentials,"Accept":ACCEPT},
                  json={"jsonrpc":"2.0","id":1,"method":"ping"})
    assert r.status_code==401


def test_proxy_runtime_logs_dont_store_raw_log_message(client):
    import logging
    fake_secret="sk-PRIVATETOKEN123456789"
    logging.getLogger("compute_plane").error("Failed with %s",fake_secret)
    events=mcp_logging.events(limit=20)
    assert events["events"]
    assert fake_secret not in json.dumps(events)
    assert "source" in events


def test_ui_mcp_page_requires_admin(client):
    assert client.get("/admin/mcp").status_code==401
    r=client.get("/admin/mcp",auth=ADMIN)
    assert r.status_code==200
    assert "/mcp" in r.text
