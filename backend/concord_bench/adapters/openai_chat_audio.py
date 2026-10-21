"""`openai` / `chat.completions` with an `input_audio` part: audio LLMs used as text-only ASR."""

from __future__ import annotations

import base64
import re
from typing import Any

import numpy as np

from ..datasets.audio import wav_bytes
from .base import Adapter, register

PROMPT = ("Transcribe the speech in this audio exactly as spoken. Reply with the transcript only: no quotes, "
          "no labels, no commentary.")
TIMEOUT_S = 600.0
_THINK = re.compile(r"<think>.*?</think>", re.S)


@register
class OpenAIChatAudio(Adapter):
    kind = "asr"
    contract = "openai"
    api = "chat.completions"

    def __init__(self, service: str, options: dict[str, Any] | None = None, *, client=None):
        super().__init__(service, {"timing": "text", "prompt": PROMPT, **(options or {})})
        self._client = client

    def client(self):
        if self._client is None:
            import aicore

            self._client = aicore.client(self.service, timeout=TIMEOUT_S, max_retries=0)
        return self._client

    def messages(self, audio: np.ndarray) -> list[dict[str, Any]]:
        b64 = base64.b64encode(wav_bytes(audio)).decode()
        return [{"role": "user", "content": [
            {"type": "text", "text": self.options["prompt"]},
            {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        ]}]

    def call(self, audio: np.ndarray) -> Any:
        import aicore

        resp = self.client().chat.completions.with_raw_response.create(
            model=aicore.model_name(self.service), messages=self.messages(audio), temperature=0.0, max_tokens=2048,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        return resp.http_response.json()

    def parse(self, raw: Any, duration: float) -> dict[str, Any]:
        return {"text": parse_chat_text(raw), "words": None, "segments": None}


def parse_chat_text(raw: Any) -> str:
    try:
        content = raw["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = _THINK.sub("", content).strip()
    return content.strip().strip('"').strip()
