import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("sewasetu_smsgw", ROOT / "smsgw" / "app.py")
sms_gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sms_gateway)


def test_inbox_and_console_require_authentication(monkeypatch):
    monkeypatch.setattr(
        sms_gateway, "SMS_GATEWAY_TOKEN", "test-gateway-token-for-unit-tests-123"
    )
    client = sms_gateway.app.test_client()

    assert client.get("/api/messages").status_code == 401
    assert client.get("/").status_code == 401


def test_gateway_endpoints_accept_valid_internal_token(monkeypatch):
    monkeypatch.setattr(
        sms_gateway, "SMS_GATEWAY_TOKEN", "test-gateway-token-for-unit-tests-123"
    )
    client = sms_gateway.app.test_client()
    headers = {"Authorization": "Bearer test-gateway-token-for-unit-tests-123"}

    assert client.get("/api/messages", headers=headers).status_code == 200
    assert client.get("/", headers=headers).status_code == 200


def test_gateway_inbox_fails_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(sms_gateway, "SMS_GATEWAY_TOKEN", "")
    response = sms_gateway.app.test_client().get("/api/messages")

    assert response.status_code == 503
