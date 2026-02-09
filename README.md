# Audiobook API

ePub/PDF/DOCX/TXT to audiobook conversion service powered by [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS).

Converts ebooks into **M4B audiobooks** (with chapter markers, cover art, and synchronized lyrics) or **MP3 ZIP archives** (per-chapter MP3s with companion LRC files).

## Architecture

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Client      │      │  audiobook-api   │      │  qwen3-tts      │
│  (curl/app)  │─────▶│  :8766           │─────▶│  :8765          │
│              │◀─────│  Job queue +     │◀─────│  TTS generation │
└─────────────┘      │  orchestration   │      └─────────────────┘
                      └────────┬─────────┘
                               │
                          ┌────▼─────┐
                          │  ffmpeg  │
                          │  M4B/MP3 │
                          │  assembly│
                          └──────────┘
```

- **Async job queue**: Upload a file, get a job ID, poll for progress
- **MD5 chunk caching**: Resume failed jobs without re-generating completed chunks
- **~1200-word chunks**: Sentence-boundary splitting for natural prosody
- **Preset voices** (Aiden, Vivian, etc.) or **voice cloning** from a reference WAV

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- [ffmpeg](https://ffmpeg.org/) (for audio encoding)
- Running [Qwen3-TTS server](../qwen3-tts) on port 8765

## Quick Start

```bash
# Install dependencies
uv sync

# Start the server (requires qwen3-tts running on :8765)
uv run python server.py
```

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /convert` | multipart | Upload file, start conversion |
| `GET /jobs` | — | List all jobs |
| `GET /jobs/{id}` | — | Job status + progress |
| `GET /jobs/{id}/download` | — | Download completed audiobook |
| `DELETE /jobs/{id}` | — | Cancel / cleanup |
| `GET /health` | — | Service + TTS health check |

### `POST /convert`

Multipart form fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | file | required | ePub, PDF, DOCX, or TXT (max 100 MB) |
| `voice` | string | `"Aiden"` | Preset voice name |
| `language` | string | `"English"` | `"English"` or `"French"` |
| `format` | string | `"m4b"` | `"m4b"` or `"mp3"` |
| `ref_audio` | file | — | Reference WAV for voice cloning (overrides `voice`) |

### Examples

**Convert with preset voice:**
```bash
curl -X POST http://localhost:8766/convert \
  -F "file=@book.epub" \
  -F "voice=Aiden" \
  -F "format=m4b"
```

**Convert with voice cloning:**
```bash
curl -X POST http://localhost:8766/convert \
  -F "file=@book.epub" \
  -F "ref_audio=@narrator-sample.wav" \
  -F "format=m4b"
```

**Check progress:**
```bash
curl http://localhost:8766/jobs/abc123def456
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

**Download result:**
```bash
curl -o audiobook.m4b http://localhost:8766/jobs/abc123def456/download
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

| Book size | Chunks | Audio length | Processing time |
|-----------|--------|-------------|-----------------|
| Short (30K words) | ~25 | ~3.3 hrs | ~1.7 hrs |
| Medium (80K words) | ~67 | ~8.9 hrs | ~4.5 hrs |
| Long (120K words) | ~100 | ~13.3 hrs | ~6.7 hrs |

Cached chunks are skipped on retry, so restarting a failed job is fast.

## Project Structure

```
audiobook-api/
├── server.py         # FastAPI app, routes, IP middleware
├── extractor.py      # ePub/PDF/DOCX/TXT → chapters + cover art
├── chunker.py        # Chapter text → ~1200-word chunks
├── tts_client.py     # Async HTTP client for Qwen3-TTS
├── sync_text.py      # LRC lyrics from chunk timing data
├── assembler.py      # WAV → M4B/MP3 via pydub + ffmpeg + mutagen
├── converter.py      # Pipeline orchestrator with caching
├── jobs.py           # SQLite job store
├── pyproject.toml    # Dependencies (uv)
└── data/             # Runtime (gitignored)
    ├── uploads/      # Uploaded source files
    ├── cache/        # MD5-keyed WAV chunk cache
    └── output/       # Final audiobooks per job
```

## Network Security

Access restricted to localhost (`127.0.0.0/8`) and Tailscale (`100.64.0.0/10`) via IP allowlist middleware — same policy as the TTS server.

## Available Voices

Aiden, Dylan, Eric, Ryan, Vivian, Serena, Uncle Fu, Ono Anna, Sohee

## License

Private project.
