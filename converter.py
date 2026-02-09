# ABOUTME: Pipeline orchestrator for audiobook conversion
# ABOUTME: Coordinates extraction → chunking → TTS → assembly with MD5 caching
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from pathlib import Path

from pydub import AudioSegment

import jobs
from assembler import (
    _get_wav_duration_secs,
    assemble_chapter_wav,
    assemble_m4b,
    assemble_mp3_zip,
)
from chunker import chunk_text
from extractor import extract
from sync_text import ChapterTiming, ChunkTiming
from tts_client import TTSClient

logger = logging.getLogger("audiobook-api.converter")

CACHE_DIR = Path("data/cache")
OUTPUT_DIR = Path("data/output")


def _cache_key(text: str, voice: str, language: str, use_clone: bool) -> str:
    """MD5 hash for chunk caching."""
    content = f"{text}|{voice}|{language}|{use_clone}"
    return hashlib.md5(content.encode()).hexdigest()


def _get_cached(key: str) -> bytes | None:
    """Retrieve cached WAV bytes by key."""
    path = CACHE_DIR / f"{key}.wav"
    if path.exists():
        return path.read_bytes()
    return None


def _save_cache(key: str, wav_bytes: bytes):
    """Save WAV bytes to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.wav"
    path.write_bytes(wav_bytes)


async def convert(
    job_id: str,
    file_path: Path,
    voice: str,
    language: str,
    fmt: str,
    ref_audio_b64: str | None,
    ref_text: str | None = None,
):
    """Run the full conversion pipeline."""
    tts = TTSClient()
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    use_clone = ref_audio_b64 is not None

    try:
        # 1. Extract text + cover art
        await jobs.update_status(job_id, "extracting")
        result = extract(file_path)
        await jobs.update_chapters_total(job_id, len(result.chapters))
        logger.info("Job %s: extracted %d chapters from %s", job_id, len(result.chapters), file_path.name)

        # 1b. Auto-transcribe reference audio if voice cloning without ref_text
        if use_clone and not ref_text:
            import base64
            ref_audio_raw = base64.b64decode(ref_audio_b64)
            ref_text = await tts.transcribe(ref_audio_raw)
            logger.info("Job %s: auto-transcribed ref_audio: %s", job_id, ref_text[:100])

        # 2. Generate audio per chapter
        await jobs.update_status(job_id, "generating")
        chapter_audios: list[AudioSegment] = []
        chapter_timings: list[ChapterTiming] = []

        for i, chapter in enumerate(result.chapters):
            chunks = chunk_text(chapter.text)
            await jobs.update_chunk_progress(job_id, 0, len(chunks))

            chunk_wavs: list[bytes] = []
            chunk_timings: list[ChunkTiming] = []
            paragraph_breaks: list[bool] = []

            for j, chunk in enumerate(chunks):
                cache_key = _cache_key(chunk.text, voice, language, use_clone)
                cached = _get_cached(cache_key)

                if cached:
                    wav_bytes = cached
                    logger.debug("Job %s: chunk %d/%d (ch %d) from cache", job_id, j + 1, len(chunks), i + 1)
                else:
                    logger.info("Job %s: generating chunk %d/%d (ch %d/%d, %d words)",
                                job_id, j + 1, len(chunks), i + 1, len(result.chapters), len(chunk.text.split()))
                    if use_clone:
                        wav_bytes = await tts.generate_clone(chunk.text, ref_audio_b64, language, ref_text=ref_text)
                    else:
                        wav_bytes = await tts.generate_preset(chunk.text, voice, language)
                    _save_cache(cache_key, wav_bytes)

                chunk_wavs.append(wav_bytes)
                duration = _get_wav_duration_secs(wav_bytes)
                chunk_timings.append(ChunkTiming(sentences=chunk.sentences, duration_secs=duration))
                paragraph_breaks.append(chunk.paragraph_break)

                await jobs.update_chunk_progress(job_id, j + 1, len(chunks))

            # Assemble chapter WAV
            chapter_audio, _ = assemble_chapter_wav(chunk_wavs, paragraph_breaks)
            chapter_audios.append(chapter_audio)
            chapter_timings.append(ChapterTiming(title=chapter.title, chunks=chunk_timings))

            await jobs.update_chapter_progress(job_id, i + 1)
            logger.info("Job %s: chapter %d/%d complete", job_id, i + 1, len(result.chapters))

        # 3. Assemble final audiobook
        await jobs.update_status(job_id, "assembling")

        if fmt == "m4b":
            output = assemble_m4b(
                job_dir, chapter_audios, result.chapters,
                result.metadata, result.cover_image, chapter_timings,
            )
        else:
            output = assemble_mp3_zip(
                job_dir, chapter_audios, result.chapters,
                result.metadata, result.cover_image, chapter_timings,
            )

        await jobs.update_status(job_id, "completed")
        logger.info("Job %s: completed — %s", job_id, output)

    except asyncio.CancelledError:
        logger.info("Job %s: cancelled", job_id)
        await jobs.update_status(job_id, "cancelled")
    except Exception as e:
        logger.exception("Job %s failed: %s", job_id, e)
        await jobs.update_status(job_id, "failed", error=str(e))
    finally:
        await tts.close()
