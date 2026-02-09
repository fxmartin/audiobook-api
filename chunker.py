# ABOUTME: Splits chapter text into ~1200-word chunks at sentence boundaries
# ABOUTME: Preserves paragraph breaks for silence insertion during audio assembly
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_WORDS = 300   # Conservative for MPS fp32 voice cloning
MAX_WORDS = 400      # Hard ceiling before forcing a split

# Sentence boundary: after .!? followed by whitespace
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Fallback split: commas, semicolons, colons, em-dashes
CLAUSE_SPLIT = re.compile(r"(?<=[,;:\u2014])\s+")


@dataclass
class Chunk:
    text: str
    paragraph_break: bool  # True if chunk ends at a paragraph boundary
    sentences: list[str]   # Individual sentences for LRC timing


def chunk_text(text: str) -> list[Chunk]:
    """Split text into ~1200-word chunks at sentence boundaries."""
    # Split into paragraphs first (for paragraph-break tracking)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # Collect all sentences with their paragraph membership
    sentence_records: list[tuple[str, bool]] = []  # (sentence, is_last_in_paragraph)
    for para in paragraphs:
        sentences = SENTENCE_SPLIT.split(para)
        sentences = [s.strip() for s in sentences if s.strip()]
        for i, sent in enumerate(sentences):
            is_last = i == len(sentences) - 1
            sentence_records.append((sent, is_last))

    # Greedily accumulate sentences into chunks
    chunks: list[Chunk] = []
    current_sentences: list[str] = []
    current_words = 0
    ends_paragraph = False

    for sent, is_last_in_para in sentence_records:
        word_count = len(sent.split())

        # If a single sentence exceeds MAX_WORDS, split it on clause boundaries
        if word_count > MAX_WORDS:
            # Flush current buffer first
            if current_sentences:
                chunks.append(Chunk(
                    text=" ".join(current_sentences),
                    paragraph_break=ends_paragraph,
                    sentences=list(current_sentences),
                ))
                current_sentences = []
                current_words = 0

            sub_sentences = CLAUSE_SPLIT.split(sent)
            for sub in sub_sentences:
                sub = sub.strip()
                if not sub:
                    continue
                sub_words = len(sub.split())
                if current_words + sub_words > TARGET_WORDS and current_sentences:
                    chunks.append(Chunk(
                        text=" ".join(current_sentences),
                        paragraph_break=False,
                        sentences=list(current_sentences),
                    ))
                    current_sentences = []
                    current_words = 0
                current_sentences.append(sub)
                current_words += sub_words
            ends_paragraph = is_last_in_para
            continue

        # Would this sentence push us over target?
        if current_words + word_count > TARGET_WORDS and current_sentences:
            chunks.append(Chunk(
                text=" ".join(current_sentences),
                paragraph_break=ends_paragraph,
                sentences=list(current_sentences),
            ))
            current_sentences = []
            current_words = 0

        current_sentences.append(sent)
        current_words += word_count
        ends_paragraph = is_last_in_para

    # Flush remaining
    if current_sentences:
        chunks.append(Chunk(
            text=" ".join(current_sentences),
            paragraph_break=True,
            sentences=list(current_sentences),
        ))

    return chunks
