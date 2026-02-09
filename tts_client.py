# ABOUTME: Async HTTP client for Qwen3-TTS server (port 8765)
# ABOUTME: Supports preset voice and voice cloning endpoints with retry logic
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("audiobook-api.tts")

TTS_BASE_URL = "http://127.0.0.1:8765"
STT_BASE_URL = "http://127.0.0.1:8766"
REQUEST_TIMEOUT = 600.0  # 10 min for long-form chunks
MAX_RETRIES = 3
BACKOFF_SECS = [5, 10, 20]

# ~300 words ≈ 2 min audio ≈ 1440 tokens at 12Hz codec
# Conservative for MPS fp32 voice cloning; preset voices can handle more
AUDIOBOOK_MAX_TOKENS_CLONE = 1440
AUDIOBOOK_MAX_TOKENS_PRESET = 3600  # ~5 min audio


class TTSClient:
    """Async client for Qwen3-TTS server."""

    def __init__(self, base_url: str = TTS_BASE_URL):
        self.base_url = base_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make HTTP request with exponential backoff retry on 5xx/timeout."""
        import asyncio

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                client = await self._get_client()
                resp = await client.request(method, path, **kwargs)
                if resp.status_code < 500:
                    resp.raise_for_status()
                    return resp
                last_exc = httpx.HTTPStatusError(
                    f"Server error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_exc = e

            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_SECS[attempt]
                logger.warning("TTS request failed (attempt %d/%d), retrying in %ds: %s",
                               attempt + 1, MAX_RETRIES, wait, last_exc)
                await asyncio.sleep(wait)

        raise last_exc  # type: ignore[misc]

    async def generate_preset(
        self,
        text: str,
        voice: str = "Aiden",
        language: str = "English",
        max_new_tokens: int = AUDIOBOOK_MAX_TOKENS_PRESET,
    ) -> bytes:
        """Generate TTS audio using a preset voice. Returns WAV bytes."""
        resp = await self._request_with_retry(
            "POST",
            "/tts",
            json={
                "text": text,
                "voice": voice,
                "language": language,
                "max_new_tokens": max_new_tokens,
            },
        )
        return resp.content

    async def generate_clone(
        self,
        text: str,
        ref_audio_b64: str,
        language: str = "English",
        max_new_tokens: int = AUDIOBOOK_MAX_TOKENS_CLONE,
        ref_text: str | None = None,
    ) -> bytes:
        """Generate TTS audio using voice cloning. Returns WAV bytes."""
        payload: dict = {
            "text": text,
            "ref_audio": ref_audio_b64,
            "language": language,
            "max_new_tokens": max_new_tokens,
        }
        if ref_text:
            payload["ref_text"] = ref_text
        resp = await self._request_with_retry(
            "POST",
            "/tts/clone",
            json=payload,
        )
        return resp.content

    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav") -> str:
        """Transcribe audio via Whisper STT server. Returns transcript text."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as stt:
            resp = await stt.post(
                f"{STT_BASE_URL}/v1/audio/transcriptions",
                files={"file": (filename, audio_bytes, "audio/wav")},
                data={"model": "large-v3-turbo"},
            )
            resp.raise_for_status()
            return resp.json()["text"]

    async def health_check(self) -> dict:
        """Check TTS server health."""
        client = await self._get_client()
        resp = await client.get("/health", timeout=5.0)
        resp.raise_for_status()
        return resp.json()
