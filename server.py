# ABOUTME: FastAPI server for ePub/PDF/DOCX/TXT to audiobook conversion
# ABOUTME: Orchestrates text extraction, Qwen3-TTS generation, and M4B/MP3 assembly on port 8766
# ABOUTME: Restricted to localhost and Tailscale IPs (100.64.0.0/10)
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import jobs
from converter import convert
from tts_client import TTSClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("audiobook-api")

UPLOAD_DIR = Path("data/uploads")
OUTPUT_DIR = Path("data/output")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

ALLOWED_EXTENSIONS = {".epub", ".pdf", ".docx", ".txt"}
ALLOWED_FORMATS = {"m4b", "mp3"}

# Same IP allowlist as qwen3-tts
ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100.64.0.0/10"),
]

app = FastAPI(title="Audiobook API", version="0.1.0")

# Track running background tasks to prevent GC
_running_tasks: dict[str, asyncio.Task] = {}


@app.middleware("http")
async def restrict_ip(request: Request, call_next):
    """Reject requests not from localhost or Tailscale."""
    client_ip = ipaddress.ip_address(request.client.host)
    if not any(client_ip in network for network in ALLOWED_NETWORKS):
        logger.warning("Blocked request from %s", client_ip)
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)


@app.on_event("startup")
async def startup():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    Path("data/cache").mkdir(parents=True, exist_ok=True)
    await jobs.init_db()
    logger.info("Audiobook API started on port 8767")


@app.get("/health")
async def health():
    """Service health + TTS dependency check."""
    tts = TTSClient()
    tts_status = "unknown"
    tts_detail = None
    try:
        tts_health = await tts.health_check()
        tts_status = tts_health.get("status", "unknown")
    except Exception as e:
        tts_status = "unreachable"
        tts_detail = str(e)
    finally:
        await tts.close()

    return {
        "status": "ok" if tts_status in ("ok", "degraded") else "degraded",
        "tts_server": {
            "status": tts_status,
            "error": tts_detail,
        },
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "supported_formats": list(ALLOWED_EXTENSIONS),
        "output_formats": list(ALLOWED_FORMATS),
    }


@app.post("/convert")
async def start_conversion(
    file: UploadFile = File(...),
    voice: str = Form("Aiden"),
    language: str = Form("English"),
    format: str = Form("m4b"),
    ref_audio: UploadFile | None = File(None),
):
    """Upload a file and start audiobook conversion."""
    # Validate format
    if format not in ALLOWED_FORMATS:
        raise HTTPException(400, f"Unsupported format: {format}. Use: {ALLOWED_FORMATS}")

    # Validate file extension
    filename = file.filename or "unknown.txt"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}. Use: {ALLOWED_EXTENSIONS}")

    # Read and validate file size
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large: {len(content)} bytes (max {MAX_UPLOAD_BYTES})")

    # Create job
    job_id = jobs.new_job_id()

    # Save uploaded file
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = job_upload_dir / filename
    file_path.write_bytes(content)

    # Handle reference audio for voice cloning
    ref_audio_b64 = None
    if ref_audio and ref_audio.filename:
        ref_content = await ref_audio.read()
        ref_audio_b64 = base64.b64encode(ref_content).decode("ascii")

    await jobs.create_job(job_id, filename, format, voice, language, use_clone=ref_audio_b64 is not None)

    # Launch background conversion (ref_text auto-transcribed via Whisper STT)
    task = asyncio.create_task(convert(job_id, file_path, voice, language, format, ref_audio_b64))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(job_id, None))

    logger.info("Job %s created: %s → %s (voice=%s, lang=%s, clone=%s)",
                job_id, filename, format, voice, language, ref_audio_b64 is not None)

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs")
async def list_all_jobs():
    """List all jobs with status."""
    all_jobs = await jobs.list_jobs()
    return [_format_job(j) for j in all_jobs]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get detailed job status + progress."""
    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return _format_job(job)


@app.get("/jobs/{job_id}/download")
async def download_job(job_id: str):
    """Download completed audiobook."""
    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    if job["status"] != "completed":
        raise HTTPException(400, f"Job not completed (status: {job['status']})")

    job_dir = OUTPUT_DIR / job_id
    fmt = job["format"]

    if fmt == "m4b":
        output_file = job_dir / "audiobook.m4b"
        media_type = "audio/mp4"
        dl_filename = Path(job["filename"]).stem + ".m4b"
    else:
        output_file = job_dir / "audiobook.zip"
        media_type = "application/zip"
        dl_filename = Path(job["filename"]).stem + ".zip"

    if not output_file.exists():
        raise HTTPException(500, "Output file missing")

    return FileResponse(
        str(output_file),
        media_type=media_type,
        filename=dl_filename,
    )


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a running job. Keeps cached chunks for future resumption."""
    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")

    if job["status"] in ("completed", "failed", "cancelled"):
        return {"job_id": job_id, "status": job["status"], "message": "Job already finished"}

    # Cancel the background task
    task = _running_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        logger.info("Cancelled running task for job %s", job_id)

    await jobs.update_status(job_id, "cancelled")
    return {"job_id": job_id, "status": "cancelled"}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Cancel a running job or clean up a completed one."""
    job = await jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")

    # Cancel running task
    task = _running_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        logger.info("Cancelled running task for job %s", job_id)

    # Clean up files
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    upload_dir = UPLOAD_DIR / job_id
    if upload_dir.exists():
        shutil.rmtree(upload_dir)

    await jobs.delete_job(job_id)
    return {"deleted": job_id}


def _format_job(job: dict) -> dict:
    """Format a job row for API response."""
    # Calculate progress percentage
    chapters_total = job.get("chapters_total", 0) or 0
    chapters_done = job.get("chapters_done", 0) or 0
    chunks_total = job.get("chunks_current_total", 0) or 0
    chunks_done = job.get("chunks_current_done", 0) or 0

    percent = 0.0
    if chapters_total > 0:
        # Weight: each chapter equally, current chapter proportional by chunks
        completed_weight = chapters_done
        if chunks_total > 0 and chapters_done < chapters_total:
            completed_weight += chunks_done / chunks_total
        percent = (completed_weight / chapters_total) * 100

    # ETA estimation
    created = job.get("created_at", "")
    elapsed_secs = 0.0
    eta_secs = None
    if created:
        from datetime import datetime, timezone
        try:
            start = datetime.fromisoformat(created)
            elapsed_secs = (datetime.now(timezone.utc) - start).total_seconds()
            if percent > 0:
                eta_secs = round(elapsed_secs * (100 - percent) / percent)
        except (ValueError, TypeError):
            pass

    return {
        "job_id": job["id"],
        "status": job["status"],
        "filename": job.get("filename"),
        "format": job.get("format"),
        "voice": job.get("voice"),
        "language": job.get("language"),
        "progress": {
            "chapters_total": chapters_total,
            "chapters_done": chapters_done,
            "chunks_current_chapter": {
                "done": chunks_done,
                "total": chunks_total,
            },
            "percent": round(percent, 1),
            "elapsed_secs": round(elapsed_secs),
            "eta_secs": eta_secs,
        },
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8767)
