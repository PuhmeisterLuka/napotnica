"""Groq client wrapper: JSON-only completions, validated against a pydantic schema.

Two independent retry policies:
  - transient 429s: exponential backoff, up to `max_retries` attempts;
  - a schema-invalid reply: exactly one retry with the validation error fed back,
    then give up by raising LLMError so the caller can mark the job unscored.
The model comes from GROQ_MODEL; the default is a current Groq production model.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "llama-3.3-70b-versatile"

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 3,
        client=None,
        backoff_base: float = 1.0,
    ):
        self.model = model or os.getenv("GROQ_MODEL") or DEFAULT_MODEL
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        if client is not None:
            self._client = client
        else:
            from groq import Groq

            # Own retry loop below, so disable the SDK's own retrying.
            self._client = Groq(api_key=api_key or os.getenv("GROQ_API_KEY"), max_retries=0)

    def _complete(self, messages: list[dict], temperature: float) -> str:
        from groq import RateLimitError

        delay = self.backoff_base
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                return resp.choices[0].message.content
            except RateLimitError:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        raise LLMError("exhausted retries without a response")  # not reached

    def complete_json(
        self,
        messages: list[dict],
        schema: Type[T],
        temperature: float = 0.0,
    ) -> T:
        content = self._complete(messages, temperature)
        try:
            return schema.model_validate_json(content)
        except ValidationError as first_error:
            retry_messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply did not match the required schema: "
                        f"{first_error}. Reply again with ONLY a valid JSON object."
                    ),
                },
            ]
            content = self._complete(retry_messages, temperature)
            try:
                return schema.model_validate_json(content)
            except ValidationError as second_error:
                raise LLMError(f"invalid JSON after one retry: {second_error}") from second_error
