import glob, ipaddress, os, shutil, socket, tempfile
from datetime import date
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

FREE_DAILY_LIMIT = 3          # swap for a real subscription check later
MAX_FILESIZE = 500 * 1024**2  # 500 MB cap per download
usage: dict[str, tuple[date, int]] = {}  # ip -> (day, count); use Redis in production

app = FastAPI()


def client_id(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else req.client.host


def remaining(cid: str) -> int:
    day, n = usage.get(cid, (date.today(), 0))
    return FREE_DAILY_LIMIT - (n if day == date.today() else 0)


def check_url(url: str):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Paste a full link starting with https://")
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "That link isn't allowed")
    except socket.gaierror:
        raise HTTPException(400, "Couldn't find that site")


class InfoReq(BaseModel):
    url: str


@app.post("/api/info")
def info(body: InfoReq, req: Request):
    check_url(body.url)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            d = ydl.extract_info(body.url, download=False)
    except Exception:
        raise HTTPException(422, "Couldn't read a video from that link")
    heights = sorted(
        {f["height"] for f in d.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )[:5]
    return {
        "title": d.get("title"),
        "thumbnail": d.get("thumbnail"),
        "duration": d.get("duration"),
        "heights": heights,
        "remaining": remaining(client_id(req)),
    }


@app.get("/api/download")
def download(url: str, req: Request, height: int | None = None):
    check_url(url)
    cid = client_id(req)
    if remaining(cid) <= 0:
        raise HTTPException(429, "Daily free limit reached")
    h = f"[height<={height}]" if height else ""
    tmp = tempfile.mkdtemp()
    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": f"{tmp}/%(title).80B.%(ext)s",
        # H.264 + AAC in MP4 so it plays in iOS Photos/Files
        "format": f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/b{h}[ext=mp4]/b{h}",
        "merge_output_format": "mp4",
        "max_filesize": MAX_FILESIZE,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        path = glob.glob(f"{tmp}/*")[0]
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(422, "Download failed. Try another quality.")
    day, n = usage.get(cid, (date.today(), 0))
    usage[cid] = (date.today(), (n if day == date.today() else 0) + 1)
    return FileResponse(
        path,
        filename=os.path.basename(path),
        background=BackgroundTask(shutil.rmtree, tmp, True),
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
