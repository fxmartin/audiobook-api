# Audiobook API

ePub/PDF/DOCX/TXT to audiobook conversion service powered by [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS).

Converts ebooks into **M4B audiobooks** (with chapter markers, cover art, and synchronized lyrics) or **MP3 ZIP archives** (per-chapter MP3s with companion LRC files).

## Architecture

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Client      │      │  audiobook-api   │      │  qwen3-tts      │
│  (curl/app)  │─────▶│  :8767           │─────▶│  :8765          │
│              │◀─────│  Job queue +     │◀─────│  TTS generation │
└─────────────┘      │  orchestration   │      └─────────────────┘
                      └──┬─────────┬────┘
                         │         │
                    ┌────▼───┐ ┌───▼──────────┐
                    │ ffmpeg │ │ whisper-stt   │
                    │ M4B/MP3│ │ :8766         │
                    │ encode │ │ auto-transcr. │
                    └────────┘ └──────────────┘
```

- **Async job queue**: Upload a file, get a job ID, poll for progress
- **MD5 chunk caching**: Resume failed jobs without re-generating completed chunks
- **~300-word chunks**: Sentence-boundary splitting (conservative for MPS fp32 voice cloning)
- **Preset voices** (Aiden, Vivian, etc.) or **voice cloning** from a reference WAV
- **Auto-transcription**: Reference audio automatically transcribed via Whisper STT (no manual `ref_text` needed)

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [ffmpeg](https://ffmpeg.org/) (for audio encoding)
- Running [Qwen3-TTS server](../qwen3-tts) on port 8765
- Running [Whisper STT server](../whisper-stt) on port 8766 (required for voice cloning)

## Quick Start

```bash
# Install dependencies
uv sync

# Start the server (requires qwen3-tts on :8765, whisper-stt on :8766 for cloning)
uv run python server.py
```

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /convert` | multipart | Upload file, start conversion |
| `GET /jobs` | — | List all jobs |
| `GET /jobs/{id}` | — | Job status + progress |
| `GET /jobs/{id}/download` | — | Download completed audiobook |
| `POST /jobs/{id}/cancel` | — | Cancel a running job (keeps cached chunks) |
| `DELETE /jobs/{id}` | — | Delete job + cleanup all files |
| `GET /health` | — | Service + TTS health check |

### `POST /convert`

Multipart form fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | file | required | ePub, PDF, DOCX, or TXT (max 100 MB) |
| `voice` | string | `"Aiden"` | Preset voice name |
| `language` | string | `"English"` | `"English"` or `"French"` |
| `format` | string | `"m4b"` | `"m4b"` or `"mp3"` |
| `ref_audio` | file | — | Reference WAV for voice cloning (auto-transcribed via Whisper STT) |

### Examples

**Convert with preset voice:**
```bash
curl -X POST http://localhost:8767/convert \
  -F "file=@book.epub" \
  -F "voice=Aiden" \
  -F "format=m4b"
```

**Convert with voice cloning:**
```bash
curl -X POST http://localhost:8767/convert \
  -F "file=@book.epub" \
  -F "ref_audio=@narrator-sample.wav" \
  -F "format=m4b"
```

The reference audio is automatically transcribed by the Whisper STT server — no manual transcript needed.

**Check progress:**
```bash
curl http://localhost:8767/jobs/abc123def456
```

```json
{
  "job_id": "abc123def456",
  "status": "generating",
  "filename": "book.epub",
  "format": "m4b",
  "voice": "Aiden",
  "progress": {
    "chapters_total": 12,
    "chapters_done": 3,
    "chunks_current_chapter": { "done": 5, "total": 8 },
    "percent": 28.5,
    "elapsed_secs": 342,
    "eta_secs": 856
  }
}
```

**Cancel a running job:**
```bash
curl -X POST http://localhost:8767/jobs/abc123def456/cancel
```

Cached chunks are preserved — re-submitting the same book will skip already-generated chunks.

**Download result:**
```bash
curl -o audiobook.m4b http://localhost:8767/jobs/abc123def456/download
```

## Output Features

### M4B
- AAC 128 kbps
- Chapter markers with titles
- Cover art from ePub (visible in Finder, Apple Music, Apple Books)
- Metadata: title, author, genre, year, description
- Synchronized LRC lyrics (scrolling text in Apple Music)

### MP3 ZIP
- MP3 192 kbps, one file per chapter
- ID3 tags: title, artist, album, track number, genre, cover art
- Companion `.lrc` files per chapter
- Cover image included in ZIP

## Processing Estimates (M3 Max, 48 GB)

Chunk sizes are tuned for MPS fp32 memory constraints: ~300 words per chunk.

### Preset Voice

| Book size | Chunks | Est. processing time |
|-----------|--------|---------------------|
| Short (30K words) | ~100 | ~3 hrs |
| Medium (80K words) | ~270 | ~8 hrs |
| Long (120K words) | ~400 | ~12 hrs |

### Voice Cloning

Voice cloning is ~3x slower than preset voices due to ICL reference processing:

| Book size | Chunks | Est. processing time |
|-----------|--------|---------------------|
| Short (30K words) | ~100 | ~8 hrs |
| Medium (80K words) | ~270 | ~22 hrs |
| Long (120K words) | ~400 | ~33 hrs |

Cached chunks are skipped on retry, so restarting a failed job is fast.

## Project Structure

```
audiobook-api/
├── server.py         # FastAPI app on :8767, routes, IP middleware
├── extractor.py      # ePub/PDF/DOCX/TXT → chapters + cover art + metadata
├── chunker.py        # Chapter text → ~300-word sentence-boundary chunks
├── tts_client.py     # Async HTTP client for Qwen3-TTS + Whisper STT
├── sync_text.py      # LRC lyrics from chunk timing data
├── assembler.py      # WAV → M4B/MP3 via pydub + ffmpeg + mutagen
├── converter.py      # Pipeline orchestrator with MD5 caching
├── jobs.py           # Async SQLite job store
├── pyproject.toml    # Dependencies (uv)
└── data/             # Runtime (gitignored)
    ├── uploads/      # Uploaded source files
    ├── cache/        # MD5-keyed WAV chunk cache
    └── output/       # Final audiobooks per job
```

## Network Security

Access restricted to localhost (`127.0.0.0/8`) and Tailscale (`100.64.0.0/10`) via IP allowlist middleware — same policy as the TTS and STT servers.

## Available Voices

Aiden, Dylan, Eric, Ryan, Vivian, Serena, Uncle Fu, Ono Anna, Sohee

## License

Private project.
