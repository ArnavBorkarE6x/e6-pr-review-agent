"""Unified AI client supporting Anthropic (Claude) and OpenAI."""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


class AIClient:
    """Wrapper around AI model APIs with structured JSON output."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        model_primary: str,
        model_lightweight: str,
    ) -> None:
        self.provider = provider
        self.model_primary = model_primary
        self.model_lightweight = model_lightweight
        self._anthropic = None
        self._openai = None

        if provider == "anthropic":
            import anthropic

            self._anthropic = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            import openai

            self._openai = openai.AsyncOpenAI(api_key=api_key)

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        lightweight: bool = False,
        max_tokens: int = 4096,
    ) -> tuple[object, dict]:
        """Send a prompt and parse the response as JSON.

        Returns (parsed_json, usage_dict).
        """
        model = self.model_lightweight if lightweight else self.model_primary

        if self._anthropic:
            text, usage = await self._call_anthropic(system_prompt, user_prompt, model, max_tokens)
        else:
            text, usage = await self._call_openai(system_prompt, user_prompt, model, max_tokens)

        parsed = self._extract_json(text)
        return parsed, usage

    async def _call_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
    ) -> tuple[str, dict]:
        assert self._anthropic is not None
        resp = await self._anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return resp.content[0].text, {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "model": model,
        }

    async def _call_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
    ) -> tuple[str, dict]:
        assert self._openai is not None
        resp = await self._openai.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content or "", {
            "input_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "output_tokens": resp.usage.completion_tokens if resp.usage else 0,
            "model": model,
        }

    @staticmethod
    def _extract_json(text: str) -> object:
        """Extract JSON from a response that may be wrapped in markdown fences."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            start = 1
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == "```":
                    end = i
                    break
            cleaned = "\n".join(lines[start:end]).strip()
        return json.loads(cleaned)
