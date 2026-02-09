# ABOUTME: Generates synchronized LRC lyrics from chunk timing data
# ABOUTME: Produces full-book LRC for M4B and per-chapter LRC for MP3 companion files
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChunkTiming:
    """Timing data for a single TTS chunk."""
    sentences: list[str]
    duration_secs: float


@dataclass
class ChapterTiming:
    """Timing data for a full chapter."""
    title: str
    chunks: list[ChunkTiming]


def _format_timestamp(secs: float) -> str:
    """Format seconds as [mm:ss.xx] for LRC."""
    minutes = int(secs // 60)
    remainder = secs % 60
    return f"[{minutes:02d}:{remainder:05.2f}]"


def _estimate_sentence_timestamps(chunks: list[ChunkTiming], offset: float = 0.0) -> list[tuple[float, str]]:
    """Distribute chunk durations across sentences proportionally by word count."""
    entries: list[tuple[float, str]] = []
    current_time = offset

    for chunk in chunks:
        total_words = sum(len(s.split()) for s in chunk.sentences)
        if total_words == 0:
            current_time += chunk.duration_secs
            continue

        for sentence in chunk.sentences:
            entries.append((current_time, sentence))
            word_fraction = len(sentence.split()) / total_words
            current_time += chunk.duration_secs * word_fraction

    return entries


def generate_full_lrc(chapters: list[ChapterTiming]) -> str:
    """Generate a full-book LRC string for M4B embedding."""
    lines: list[str] = []
    offset = 0.0

    for chapter in chapters:
        # Chapter title marker
        lines.append(f"{_format_timestamp(offset)} {chapter.title}")

        entries = _estimate_sentence_timestamps(chapter.chunks, offset)
        for ts, text in entries:
            # Truncate very long sentences for LRC readability
            display = text[:200] + "..." if len(text) > 200 else text
            lines.append(f"{_format_timestamp(ts)} {display}")

        # Advance offset by total chapter duration + 3s inter-chapter silence
        chapter_duration = sum(c.duration_secs for c in chapter.chunks)
        offset += chapter_duration + 3.0

    return "\n".join(lines) + "\n"


def generate_chapter_lrc(chapter: ChapterTiming) -> str:
    """Generate a per-chapter LRC string for MP3 companion files."""
    lines: list[str] = []
    lines.append(f"{_format_timestamp(0.0)} {chapter.title}")

    entries = _estimate_sentence_timestamps(chapter.chunks, offset=0.0)
    for ts, text in entries:
        display = text[:200] + "..." if len(text) > 200 else text
        lines.append(f"{_format_timestamp(ts)} {display}")

    return "\n".join(lines) + "\n"
