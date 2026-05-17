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

class DownloadRequest(BaseModel):
    url: str
    cookies: str = None

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
    
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Prioritize mp4
        'merge_output_format': 'mp4',
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
            if not os.path.exists(downloaded_file):
                # Fallback check for common merged extensions
                if os.path.exists(downloaded_file.replace('.webm', '.mp4')):
                    downloaded_file = downloaded_file.replace('.webm', '.mp4')
                elif os.path.exists(downloaded_file.replace('.mkv', '.mp4')):
                    downloaded_file = downloaded_file.replace('.mkv', '.mp4')

            if not os.path.exists(downloaded_file):
                raise FileNotFoundError("File was not downloaded properly.")

            # Sanitize the title for the download filename
            safe_title = "".join([c for c in info_dict.get('title', 'video') if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            filename = f"{safe_title}.mp4"

            # We use a background task to delete the file after it's been streamed, 
            # but FileResponse doesn't have native background tasks in starlette easily without wrapping.
            # As a simpler approach for a stateless container (like Render), 
            # we'll just return the file. Render containers spin down or we can add a cron cleanup.
            # To avoid disk filling up, let's implement a quick cleanup of old files in TEMP_DIR.
            cleanup_old_files()

            return FileResponse(
                path=downloaded_file, 
                filename=filename, 
                media_type="video/mp4"
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
