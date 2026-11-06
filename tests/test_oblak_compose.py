"""Regression checks for the one-file Oblak staging stack."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_gateway_runs_caddy_binary_and_internal_tls():
    file = ROOT / "deploy/oblak-test-proxy-worker/docker-compose.yml"
    config = yaml.safe_load(file.read_text(encoding="utf-8"))
    gateway = config["services"]["gateway"]
    command = gateway["command"]
    assert command[:2] == ["caddy", "reverse-proxy"]
    assert "--internal-certs" in command
    assert "--from" in command and "--to" in command
    assert "ports" not in gateway, "Staging TLS gateway must not expose host ports"


def test_proxy_is_loopback_only_with_distinct_staging_resources():
    file = ROOT / "deploy/oblak-test-proxy-worker/docker-compose.yml"
    config = yaml.safe_load(file.read_text(encoding="utf-8"))
    assert config["name"] == "test-proxy-worker"
    proxy = config["services"]["proxy"]
    assert proxy["ports"] == ["127.0.0.1:18080:8000"]
    assert "worker-data" in config["volumes"]
    assert "proxy-state" in config["volumes"]
    assert "worker" in config["services"]
