import os
import uuid
import queue
import threading
import json
import asyncio
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp
import shutil

app = FastAPI(title="SOCTI OSINT Downloader API")

# Allow CORS for the frontend to hit this API directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict to your Vercel domains
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)

TEMP_DIR = "/tmp/socti_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

# Global completed jobs cache
completed_jobs = {}

from typing import Optional

class DownloadRequest(BaseModel):
    url: str
    cookies: Optional[str] = None
    quality: Optional[str] = "best"

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "osint-downloader"}

@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """
    Downloads a video from the provided URL and streams it back.
    Uses yt-dlp which supports YouTube, TikTok, Pinterest, Twitter, etc.
    """
    url = req.url
    
    # Generate a unique ID for this download to avoid collisions
    job_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DIR, f"{job_id}.%(ext)s")
    
    quality = req.quality or "best"
    
    # Map quality presets to yt-dlp format options
    format_option = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    
    if quality == '1080':
        format_option = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best'
    elif quality == '720':
        format_option = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best'
    elif quality == '480':
        format_option = 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best'
    elif quality == '360':
        format_option = 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best'
    elif quality == 'audio':
        format_option = 'bestaudio/best'

    ydl_opts = {
        'outtmpl': output_template,
        'format': format_option,
        'merge_output_format': 'mp4' if quality != 'audio' else None,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True, # Only download a single video, not the whole playlist
        'no_color': True,
        'extractor_args': {
            'youtube': ['player_client=ios,android']
        }
    }
    
    # Bypass YouTube bot protection by using cookies if available
    # We will also support passing a custom cookies string in the request
    temp_cookie_path = os.path.join(TEMP_DIR, f"cookies_{job_id}.txt")
    if req.cookies:
        with open(temp_cookie_path, "w") as f:
            f.write(req.cookies)
        ydl_opts['cookiefile'] = temp_cookie_path
    elif os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = "cookies.txt"
    
    try:
        # First try: download with requested quality format options
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                downloaded_file = ydl.prepare_filename(info_dict)
        except Exception as first_err:
            # If the format parsing or merge fails (very common for non-YouTube links like Pinterest/TikTok/etc.
            # that don't split video and audio tracks), we retry using the best single progressive stream fallback.
            print(f"Primary format download failed: {first_err}. Retrying with absolute default format settings...")
            ydl_opts_fallback = ydl_opts.copy()
            # Remove format constraints entirely so yt-dlp uses its absolute defaults
            ydl_opts_fallback.pop('format', None)
            ydl_opts_fallback.pop('merge_output_format', None)
            
            with yt_dlp.YoutubeDL(ydl_opts_fallback) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                downloaded_file = ydl.prepare_filename(info_dict)
            
            # If merging happened, the actual file might have .mp4 appended if it wasn't already
            # Or if audio-only format resulted in .m4a, .webm, or .mp3, let's find the correct file
            if not os.path.exists(downloaded_file):
                # Fallback check for common merged extensions
                for possible_ext in ['.mp4', '.m4a', '.mp3', '.webm', '.mkv']:
                    base, _ = os.path.splitext(downloaded_file)
                    if os.path.exists(base + possible_ext):
                        downloaded_file = base + possible_ext
                        break

            if not os.path.exists(downloaded_file):
                # Try to scan the TEMP_DIR for any file starting with job_id
                found = False
                for f in os.listdir(TEMP_DIR):
                    if f.startswith(job_id):
                        downloaded_file = os.path.join(TEMP_DIR, f)
                        found = True
                        break
                if not found:
                    raise FileNotFoundError("File was not downloaded properly.")

            # Get actual extension of the downloaded file
            _, ext = os.path.splitext(downloaded_file)
            ext = ext.lstrip('.') or 'mp4'

            # Sanitize the title for the download filename
            safe_title = "".join([c for c in info_dict.get('title', 'video') if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            filename = f"{safe_title}.{ext}"

            # Set correct media type
            media_type = "video/mp4"
            if ext in ['m4a', 'aac', 'mp3', 'webm', 'ogg']:
                if ext == 'mp3':
                    media_type = "audio/mpeg"
                elif ext in ['m4a', 'aac']:
                    media_type = "audio/mp4"
                else:
                    media_type = f"audio/{ext}"

            cleanup_old_files()

            return FileResponse(
                path=downloaded_file, 
                filename=filename, 
                media_type=media_type
            )

    except Exception as e:
        print(f"Download Error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to download video: {str(e)}")
    finally:
        if os.path.exists(temp_cookie_path):
            try:
                os.remove(temp_cookie_path)
            except Exception:
                pass


def cleanup_old_files():
    """Removes files older than 1 hour in the temp directory."""
    import time
    now = time.time()
    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        if os.path.isfile(file_path):
            if os.stat(file_path).st_mtime < now - 3600:
                try:
                    os.remove(file_path)
                except Exception:
                    pass


def run_yt_dlp_download(url, ydl_opts, q, fallback_opts=None):
    """
    Runs the download in a background thread and puts progress/errors onto the queue.
    """
    import re
    ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F]|(?:\x1B\[|\x9B)[0-?]*[ -/]*[@-~])')

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes') or 0
            
            percent = 0
            if total > 0:
                percent = round((downloaded / total) * 100, 1)
            else:
                p_str = d.get('_percent_str', '').strip('% ')
                try:
                    percent = float(p_str)
                except ValueError:
                    percent = 0
                    
            speed = d.get('_speed_str', '').strip()
            speed = ansi_escape.sub('', speed)
            
            eta = d.get('_eta_str', '').strip()
            eta = ansi_escape.sub('', eta)
            
            q.put({
                "status": "downloading",
                "progress": percent,
                "speed": speed,
                "eta": eta
            })
        elif d['status'] == 'finished':
            q.put({
                "status": "processing",
                "message": "Finalizing backend download..."
            })
            
    ydl_opts['progress_hooks'] = [progress_hook]
    if fallback_opts:
        fallback_opts['progress_hooks'] = [progress_hook]

    try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                downloaded_file = ydl.prepare_filename(info_dict)
        except Exception as first_err:
            if fallback_opts:
                q.put({
                    "status": "processing",
                    "message": "Format match failed. Retrying with progressive format..."
                })
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info_dict = ydl.extract_info(url, download=True)
                    downloaded_file = ydl.prepare_filename(info_dict)
            else:
                raise first_err

        if not os.path.exists(downloaded_file):
            for possible_ext in ['.mp4', '.m4a', '.mp3', '.webm', '.mkv']:
                base, _ = os.path.splitext(downloaded_file)
                if os.path.exists(base + possible_ext):
                    downloaded_file = base + possible_ext
                    break

        if not os.path.exists(downloaded_file):
            job_id = os.path.basename(ydl_opts['outtmpl']).split('.')[0]
            found = False
            for f in os.listdir(TEMP_DIR):
                if f.startswith(job_id):
                    downloaded_file = os.path.join(TEMP_DIR, f)
                    found = True
                    break
            if not found:
                raise FileNotFoundError("File was not downloaded properly.")

        q.put({
            "status": "completed",
            "downloaded_file": downloaded_file,
            "info_dict": info_dict
        })
    except Exception as e:
        q.put({
            "status": "error",
            "message": str(e)
        })


@app.post("/api/download-stream")
async def download_video_stream(req: DownloadRequest):
    """
    Initiates the download and streams real-time progress events back to the client using SSE.
    """
    url = req.url
    job_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DIR, f"{job_id}.%(ext)s")
    
    quality = req.quality or "best"
    
    format_option = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    if quality == '1080':
        format_option = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best'
    elif quality == '720':
        format_option = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best'
    elif quality == '480':
        format_option = 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best'
    elif quality == '360':
        format_option = 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best'
    elif quality == 'audio':
        format_option = 'bestaudio/best'

    ydl_opts = {
        'outtmpl': output_template,
        'format': format_option,
        'merge_output_format': 'mp4' if quality != 'audio' else None,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'no_color': True,
        'extractor_args': {
            'youtube': ['player_client=ios,android']
        }
    }
    
    temp_cookie_path = os.path.join(TEMP_DIR, f"cookies_{job_id}.txt")
    if req.cookies:
        with open(temp_cookie_path, "w") as f:
            f.write(req.cookies)
        ydl_opts['cookiefile'] = temp_cookie_path
    elif os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = "cookies.txt"

    fallback_opts = ydl_opts.copy()
    fallback_opts.pop('format', None)
    fallback_opts.pop('merge_output_format', None)

    q = queue.Queue()
    
    t = threading.Thread(
        target=run_yt_dlp_download,
        args=(url, ydl_opts, q, fallback_opts)
    )
    t.daemon = True
    t.start()

    async def event_generator():
        last_percent = -1
        while True:
            try:
                await asyncio.sleep(0.1)
                event = q.get_nowait()
            except queue.Empty:
                if not t.is_alive():
                    break
                continue
                
            status = event.get("status")
            if status == "downloading":
                progress = event.get("progress")
                if progress != last_percent:
                    last_percent = progress
                    yield f"data: {json.dumps(event)}\n\n"
            elif status in ["processing", "retrying"]:
                yield f"data: {json.dumps(event)}\n\n"
            elif status == "error":
                if os.path.exists(temp_cookie_path):
                    try: os.remove(temp_cookie_path)
                    except: pass
                yield f"data: {json.dumps(event)}\n\n"
                break
            elif status == "completed":
                if os.path.exists(temp_cookie_path):
                    try: os.remove(temp_cookie_path)
                    except: pass
                    
                downloaded_file = event.get("downloaded_file")
                info_dict = event.get("info_dict")
                
                _, ext = os.path.splitext(downloaded_file)
                ext = ext.lstrip('.') or 'mp4'
                
                safe_title = "".join([c for c in info_dict.get('title', 'video') if c.isalpha() or c.isdigit() or c==' ']).rstrip()
                filename = f"{safe_title}.{ext}"
                
                media_type = "video/mp4"
                if ext in ['m4a', 'aac', 'mp3', 'webm', 'ogg']:
                    if ext == 'mp3':
                        media_type = "audio/mpeg"
                    elif ext in ['m4a', 'aac']:
                        media_type = "audio/mp4"
                    else:
                        media_type = f"audio/{ext}"
                        
                completed_jobs[job_id] = {
                    "path": downloaded_file,
                    "filename": filename,
                    "media_type": media_type
                }
                
                yield f"data: {json.dumps({'status': 'completed', 'job_id': job_id, 'filename': filename})}\n\n"
                cleanup_old_files()
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/retrieve/{job_id}")
def retrieve_download(job_id: str, background_tasks: BackgroundTasks):
    """
    Retrieves the completed download file by its unique job ID.
    Once retrieved, the file is queued for deletion from the server disk.
    """
    if job_id not in completed_jobs:
        raise HTTPException(status_code=404, detail="Download job not found or expired.")
        
    job_info = completed_jobs[job_id]
    file_path = job_info["path"]
    filename = job_info["filename"]
    media_type = job_info["media_type"]
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File has already been removed or lost.")
        
    def delete_file(path: str, j_id: str):
        try:
            import time
            # Briefly sleep to allow the response to completely flush
            time.sleep(5)
            if os.path.exists(path):
                os.remove(path)
            if j_id in completed_jobs:
                del completed_jobs[j_id]
            print(f"Cleaned up temp file {path} post-retrieval.")
        except Exception as e:
            print(f"Error deleting file post-retrieval: {e}")
            
    background_tasks.add_task(delete_file, file_path, job_id)
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type
    )

