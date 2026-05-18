import os
import uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
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
)

TEMP_DIR = "/tmp/socti_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info to get the final filename before downloading
            info_dict = ydl.extract_info(url, download=True)
            # yt-dlp can sometimes change the extension during merging, so we get the prepared filename
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
