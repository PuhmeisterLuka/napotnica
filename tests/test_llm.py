import types

import httpx
import pytest
from groq import RateLimitError

from app.llm import LLMClient, LLMError
from app.schemas import JobScore


def _response(content: str):
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _response(item)


class FakeGroq:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(script))


def rate_limit():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


def make_client(script, **kwargs):
    fake = FakeGroq(script)
    client = LLMClient(client=fake, backoff_base=0.0, **kwargs)
    return client, fake.chat.completions


def test_valid_json_validates_in_one_call():
    client, calls = make_client(['{"score": 80, "fits": ["python", "flask"]}'])
    out = client.complete_json([{"role": "user", "content": "x"}], JobScore)
    assert isinstance(out, JobScore) and out.score == 80
    assert len(calls.calls) == 1


def test_malformed_json_triggers_exactly_one_retry():
    client, calls = make_client(['not json at all', '{"score": 50, "fits": ["x"]}'])
    out = client.complete_json([{"role": "user", "content": "x"}], JobScore)
    assert out.score == 50
    assert len(calls.calls) == 2  # original + one retry


def test_still_invalid_after_retry_raises_and_does_not_retry_again():
    client, calls = make_client(['garbage', '{"score": 999}'])  # 2nd fails schema (no fits, out of range)
    with pytest.raises(LLMError):
        client.complete_json([{"role": "user", "content": "x"}], JobScore)
    assert len(calls.calls) == 2  # no third attempt


def test_rate_limit_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    client, calls = make_client([rate_limit(), '{"score": 10, "fits": ["y"]}'])
    out = client.complete_json([{"role": "user", "content": "x"}], JobScore)
    assert out.score == 10
    assert len(calls.calls) == 2


def test_rate_limit_exhausts_after_max_retries(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _s: None)
    client, calls = make_client([rate_limit(), rate_limit(), rate_limit()], max_retries=3)
    with pytest.raises(RateLimitError):
        client.complete_json([{"role": "user", "content": "x"}], JobScore)
    assert len(calls.calls) == 3
