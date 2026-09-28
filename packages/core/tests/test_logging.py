from agenteval_core.logging import REDACTED, redact_secrets

KEY = "sk-ant-api03-AbC_dEf-123"


def run(event: dict[str, object]) -> dict[str, object]:
    return dict(redact_secrets(None, "info", dict(event)))


def test_key_values_are_redacted_anywhere() -> None:
    out = run({"event": f"calling with {KEY}", "error": f"401: bad key {KEY}",
               "nested": {"headers": [f"x {KEY}"]}})  # fmt: skip
    assert KEY not in str(out)
    assert out["event"] == f"calling with sk-ant-{REDACTED}"


def test_secret_named_fields_are_redacted_whatever_their_value() -> None:
    out = run({"api_key": "anything", "Authorization": "Bearer x",
               "headers": {"x-api-key": "v", "content-type": "json"}})  # fmt: skip
    assert out["api_key"] == REDACTED and out["Authorization"] == REDACTED
    assert out["headers"] == {"x-api-key": REDACTED, "content-type": "json"}


def test_ordinary_fields_are_untouched() -> None:
    event = {"event": "run_finished", "tokens": 480, "tokens_in": 400, "cost_usd": "0.0008",
             "run_id": "01a0e7ea"}  # fmt: skip
    assert run(event) == event
