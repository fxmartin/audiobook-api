# ABOUTME: Assembles WAV chunks into M4B (with chapters) or MP3 ZIP audiobooks
# ABOUTME: Embeds cover art, metadata tags, and LRC synchronized text via ffmpeg + mutagen
from __future__ import annotations

import io
import logging
import subprocess
import zipfile
from pathlib import Path

from pydub import AudioSegment

from extractor import BookMetadata, Chapter
from sync_text import ChapterTiming, generate_chapter_lrc, generate_full_lrc

logger = logging.getLogger("audiobook-api.assembler")

SILENCE_BETWEEN_CHUNKS_MS = 500
SILENCE_PARAGRAPH_BREAK_MS = 1500
SILENCE_BETWEEN_CHAPTERS_MS = 3000


def _get_wav_duration_secs(wav_bytes: bytes) -> float:
    """Get duration of WAV audio in seconds."""
    seg = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    return seg.duration_seconds


def assemble_chapter_wav(
    chunk_wavs: list[bytes],
    paragraph_breaks: list[bool],
) -> tuple[AudioSegment, float]:
    """Concatenate chunk WAVs into a single chapter AudioSegment.

    Returns (chapter_audio, total_duration_secs).
    """
    chapter = AudioSegment.empty()

    for i, wav_bytes in enumerate(chunk_wavs):
        segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
        if i > 0:
            # Insert appropriate silence
            if paragraph_breaks[i - 1]:
                chapter += AudioSegment.silent(duration=SILENCE_PARAGRAPH_BREAK_MS)
            else:
                chapter += AudioSegment.silent(duration=SILENCE_BETWEEN_CHUNKS_MS)
        chapter += segment

    return chapter, chapter.duration_seconds


def assemble_m4b(
    job_dir: Path,
    chapter_audios: list[AudioSegment],
    chapters: list[Chapter],
    metadata: BookMetadata,
    cover_image: bytes | None,
    chapter_timings: list[ChapterTiming],
) -> Path:
    """Assemble chapter WAVs into a single M4B with chapters, cover art, and LRC."""
    # Concatenate all chapters with inter-chapter silence
    full_audio = AudioSegment.empty()
    chapter_starts_ms: list[int] = []

    for i, audio in enumerate(chapter_audios):
        chapter_starts_ms.append(len(full_audio))
        full_audio += audio
        if i < len(chapter_audios) - 1:
            full_audio += AudioSegment.silent(duration=SILENCE_BETWEEN_CHAPTERS_MS)

    # Export concatenated WAV
    concat_wav = job_dir / "concat.wav"
    full_audio.export(str(concat_wav), format="wav")

    # Write ffmpeg metadata file with chapter markers
    metadata_file = job_dir / "metadata.txt"
    with open(metadata_file, "w") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={metadata.title}\n")
        f.write(f"artist={metadata.author}\n")
        f.write(f"album={metadata.title}\n")
        f.write(f"genre=Audiobook\n")
        if metadata.year:
            f.write(f"date={metadata.year}\n")
        if metadata.description:
            # Escape special chars for ffmetadata
            desc = metadata.description.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\n")
            f.write(f"description={desc}\n")
        f.write("\n")

        for i, title in enumerate(ch.title for ch in chapters):
            start_ms = chapter_starts_ms[i]
            # End is start of next chapter or end of audio
            if i + 1 < len(chapter_starts_ms):
                end_ms = chapter_starts_ms[i + 1]
            else:
                end_ms = len(full_audio)
            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={start_ms}\n")
            f.write(f"END={end_ms}\n")
            f.write(f"title={title}\n")
            f.write("\n")

    # Build ffmpeg command
    output_m4b = job_dir / "audiobook.m4b"
    cmd = ["ffmpeg", "-y", "-i", str(concat_wav), "-i", str(metadata_file)]

    if cover_image:
        cover_path = job_dir / "cover.jpg"
        cover_path.write_bytes(cover_image)
        cmd.extend(["-i", str(cover_path)])
        cmd.extend([
            "-map", "0:a", "-map", "2:v",
            "-c:v", "mjpeg",
            "-disposition:v:0", "attached_pic",
        ])
    else:
        cmd.extend(["-map", "0:a"])

    cmd.extend([
        "-map_metadata", "1",
        "-c:a", "aac", "-b:a", "128k",
        str(output_m4b),
    ])

    logger.info("Running ffmpeg for M4B: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ffmpeg M4B failed: %s", result.stderr)
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")

    # Post-process with mutagen to embed LRC lyrics and additional metadata
    _embed_m4b_metadata(output_m4b, metadata, chapter_timings)

    # Cleanup temp files
    concat_wav.unlink(missing_ok=True)
    metadata_file.unlink(missing_ok=True)

    logger.info("M4B assembled: %s (%.1f MB)", output_m4b, output_m4b.stat().st_size / 1e6)
    return output_m4b


def _embed_m4b_metadata(
    m4b_path: Path,
    metadata: BookMetadata,
    chapter_timings: list[ChapterTiming],
):
    """Embed LRC lyrics and MP4 tags via mutagen."""
    from mutagen.mp4 import MP4

    mp4 = MP4(str(m4b_path))

    # Standard MP4 tags
    mp4["\xa9nam"] = [metadata.title]
    mp4["\xa9ART"] = [metadata.author]
    mp4["\xa9alb"] = [metadata.title]
    mp4["\xa9gen"] = ["Audiobook"]
    if metadata.year:
        mp4["\xa9day"] = [metadata.year]
    if metadata.description:
        mp4["desc"] = [metadata.description[:255]]

    # Embed LRC synchronized lyrics
    lrc_text = generate_full_lrc(chapter_timings)
    mp4["\xa9lyr"] = [lrc_text]

    mp4.save()
    logger.info("Embedded mutagen metadata + LRC lyrics in M4B")


def assemble_mp3_zip(
    job_dir: Path,
    chapter_audios: list[AudioSegment],
    chapters: list[Chapter],
    metadata: BookMetadata,
    cover_image: bytes | None,
    chapter_timings: list[ChapterTiming],
) -> Path:
    """Assemble per-chapter MP3s + LRC files into a ZIP archive."""
    mp3_dir = job_dir / "mp3s"
    mp3_dir.mkdir(exist_ok=True)

    mp3_files: list[Path] = []
    lrc_files: list[Path] = []
    total_chapters = len(chapter_audios)

    for i, (audio, chapter, timing) in enumerate(zip(chapter_audios, chapters, chapter_timings)):
        chapter_num = i + 1
        chapter_wav = mp3_dir / f"chapter_{chapter_num:02d}.wav"
        audio.export(str(chapter_wav), format="wav")

        chapter_mp3 = mp3_dir / f"chapter_{chapter_num:02d}.mp3"

        # Build ffmpeg command for MP3
        cmd = ["ffmpeg", "-y", "-i", str(chapter_wav)]

        if cover_image:
            cover_path = job_dir / "cover.jpg"
            if not cover_path.exists():
                cover_path.write_bytes(cover_image)
            cmd.extend([
                "-i", str(cover_path),
                "-map", "0:a", "-map", "1",
                "-c:a", "libmp3lame", "-b:a", "192k",
                "-id3v2_version", "3",
                "-metadata:s:v", "title=Cover",
            ])
        else:
            cmd.extend([
                "-map", "0:a",
                "-c:a", "libmp3lame", "-b:a", "192k",
                "-id3v2_version", "3",
            ])

        cmd.append(str(chapter_mp3))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("ffmpeg MP3 chapter %d failed: %s", chapter_num, result.stderr)
            raise RuntimeError(f"ffmpeg MP3 failed for chapter {chapter_num}: {result.stderr[:500]}")

        # Embed ID3 tags via mutagen
        _embed_mp3_metadata(chapter_mp3, chapter, metadata, chapter_num, total_chapters, cover_image)

        mp3_files.append(chapter_mp3)
        chapter_wav.unlink(missing_ok=True)

        # Generate companion LRC
        lrc_content = generate_chapter_lrc(timing)
        lrc_path = mp3_dir / f"chapter_{chapter_num:02d}.lrc"
        lrc_path.write_text(lrc_content, encoding="utf-8")
        lrc_files.append(lrc_path)

    # Create ZIP
    output_zip = job_dir / "audiobook.zip"
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for mp3 in mp3_files:
            zf.write(mp3, mp3.name)
        for lrc in lrc_files:
            zf.write(lrc, lrc.name)
        if cover_image:
            zf.writestr("cover.jpg", cover_image)

    # Cleanup
    for f in mp3_files + lrc_files:
        f.unlink(missing_ok=True)
    mp3_dir.rmdir()

    logger.info("MP3 ZIP assembled: %s (%.1f MB)", output_zip, output_zip.stat().st_size / 1e6)
    return output_zip


def _embed_mp3_metadata(
    mp3_path: Path,
    chapter: Chapter,
    metadata: BookMetadata,
    chapter_num: int,
    total_chapters: int,
    cover_image: bytes | None,
):
    """Embed ID3 tags in an MP3 file via mutagen."""
    from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK

    try:
        tags = ID3(str(mp3_path))
    except Exception:
        tags = ID3()

    tags["TIT2"] = TIT2(encoding=3, text=[chapter.title])
    tags["TPE1"] = TPE1(encoding=3, text=[metadata.author])
    tags["TALB"] = TALB(encoding=3, text=[metadata.title])
    tags["TRCK"] = TRCK(encoding=3, text=[f"{chapter_num}/{total_chapters}"])
    tags["TCON"] = TCON(encoding=3, text=["Audiobook"])
    if metadata.year:
        tags["TDRC"] = TDRC(encoding=3, text=[metadata.year])

    if cover_image:
        tags["APIC"] = APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,  # Cover (front)
            desc="Cover",
            data=cover_image,
        )

    tags.save(str(mp3_path))
