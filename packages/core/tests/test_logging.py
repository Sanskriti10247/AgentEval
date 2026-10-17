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


AE_KEY = "ae_0a1b2c3d_" + "Zy9-_" * 8 + "abc"


def test_agenteval_keys_and_bearer_credentials_are_redacted() -> None:
    out = run(
        {
            "event": f"request with {AE_KEY}",
            "header": f"Authorization: Bearer {AE_KEY}",
            "other": "token Bearer abcdefghijklmnop done",
            "headers": [("authorization", f"Bearer {AE_KEY}"), (b"Authorization", AE_KEY.encode())],
            "raw": {"Authorization": f"Bearer {AE_KEY}", "nested": [AE_KEY]},
        }
    )
    assert AE_KEY not in str(out) and "Zy9-_" not in str(out) and "abcdefghijklmnop" not in str(out)
    assert out["event"] == f"request with ae_{REDACTED}"
    assert out["headers"] == [("authorization", REDACTED), (b"Authorization", REDACTED)]


def test_a_key_prefix_alone_is_an_ordinary_log_value() -> None:
    event = {"event": "api_request", "api_key_prefix": "0a1b2c3d", "api_key_name": "ci"}
    assert run(event) == event
