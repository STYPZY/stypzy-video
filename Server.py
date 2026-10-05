"""Stypzy Video v2 - run: python server.py   (needs: python -m pip install -U yt-dlp)
Files are prepared in a temp folder, then handed to the browser's own download flow."""
import atexit, json, os, re, secrets, shutil, sys, tempfile, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, DownloadError
except ImportError:
    raise SystemExit("yt-dlp is required. Install with: python -m pip install -U yt-dlp")

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(tempfile.gettempdir(), "stypzy-video")
TOKEN = secrets.token_urlsafe(24)
FFMPEG = shutil.which("ffmpeg") or (
    os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe") if os.name == "nt" else None)
if FFMPEG and not os.path.isfile(FFMPEG):
    FFMPEG = None
SLOTS, ALLOWED_HOSTS, TERMINAL = threading.Semaphore(2), set(), ("completed", "cancelled", "error")
TTL = 1800  # seconds a finished file stays available


def clean_error(e):
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", l).strip() for l in str(e).splitlines() if l.strip()]
    return "\n".join(lines[-3:]) or "Something went wrong."


def fmt_duration(sec):
    if not sec:
        return None
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Silent:
    debug = info = warning = error = lambda self, _: None


class Live:
    """Turns yt-dlp's log lines into friendly search status."""
    STEPS = (("Extracting URL", "Connecting to the site"), ("webpage", "Reading the page"), ("player", "Loading player data"),
             ("manifest", "Reading stream lists"), ("m3u8", "Reading stream lists"), ("Downloading", "Collecting formats"))

    def __init__(self, rec):
        self.rec, self.n = rec, 0

    def debug(self, msg):
        for key, text in self.STEPS:
            if key in msg:
                self.n += 1
                self.rec.update(message=text, percent=min(88, 12 + self.n * 11))
                break

    info = warning = error = lambda self, _: None


def build_options(job, d, staging):
    mode, pps = d.get("mode", "video"), []
    fmt = d.get("fmt") or {}
    fid = re.sub(r"[^\w.\-]", "", str(fmt.get("id", "")))
    if mode == "audio":
        target = d.get("target") if d.get("target") in ("original", "mp3", "m4a", "opus") else "mp3"
        selector, job["stages"] = fid or "ba/b", 1
        if target != "original":
            if not FFMPEG:
                raise ValueError("Converting audio needs FFmpeg. Choose 'Original' or install FFmpeg.")
            q = str(d.get("aquality")) if str(d.get("aquality")) in ("128", "192", "256", "320") else "192"
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": target, "preferredquality": q})
    elif fid:
        if fmt.get("audio"):
            selector, job["stages"] = fid, 1
        elif FFMPEG:
            selector, job["stages"] = f"{fid}+ba/b", 2      # stream copy, no re-encode
        else:
            raise ValueError("That stream has no sound and FFmpeg is missing. Pick a format marked 'With sound'.")
    else:
        h = int(d["height"]) if str(d.get("height") or "").isdigit() else None
        cap = f"[height<={h}]" if h else ""
        selector = f"bv*{cap}+ba/b{cap}" if FFMPEG else f"b{cap}"
        job["stages"] = 2 if FFMPEG else 1
    opts = {"format": selector, "outtmpl": os.path.join(staging, "%(title).120B [%(id)s].%(ext)s"),
            "windowsfilenames": True, "noplaylist": True, "quiet": True, "no_warnings": True, "logger": Silent(),
            "retries": 3, "fragment_retries": 3, "continuedl": True}
    if d.get("embed") and FFMPEG:
        opts["writethumbnail"] = True
        pps = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}] + pps + [
            {"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
    if FFMPEG:
        opts.update(ffmpeg_location=os.path.dirname(FFMPEG), merge_output_format="mp4/mkv")
    if pps:
        opts["postprocessors"] = pps
    return opts


class Jobs:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def create(self, d):
        thumb = str(d.get("thumb") or "")
        job = {"id": uuid.uuid4().hex, "dl": secrets.token_urlsafe(16), "title": str(d.get("title") or d["url"])[:200],
               "thumb": thumb if thumb.startswith("http") else "", "label": str(d.get("label") or "")[:60],
               "status": "queued", "percent": 0, "speed": None, "eta": None, "message": "Waiting for a free slot",
               "error": "", "files": [], "created": time.time(), "finished": 0, "stages": 1, "staging": None,
               "cancel": threading.Event(), "data": d}
        with self.lock:
            self.items[job["id"]] = job
        threading.Thread(target=self.run, args=(job,), daemon=True).start()
        return job["id"]

    def drop(self, k):
        j = self.items.pop(k, None)
        if j and j["staging"]:
            shutil.rmtree(j["staging"], ignore_errors=True)

    def listing(self):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL and time.time() - j["finished"] > TTL]:
                self.drop(k)
            return [{**{k: v for k, v in j.items() if k not in ("cancel", "data", "stages", "staging", "dl", "files")},
                     "files": [{"name": n, "size": s, "url": f"/file/{j['id']}/{j['dl']}/{i}"} for i, (n, s) in enumerate(j["files"])]}
                    for j in sorted(self.items.values(), key=lambda j: -j["created"])]

    def finish(self, job, status, message, **extra):
        with self.lock:
            if job["status"] in TERMINAL:
                return
            job.update(status=status, message=message, speed=None, eta=None, finished=time.time(), **extra)
            if status == "completed":
                job["percent"] = 100

    def cancel(self, key):
        with self.lock:
            j = self.items.get(key)
            if not j or j["status"] in TERMINAL:
                return False
            j["cancel"].set()
            j["message"] = "Stopping"
            return True

    def clear(self):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL]:
                self.drop(k)

    def run(self, job):
        d = job["data"]
        try:
            with SLOTS:
                def check():
                    if job["cancel"].is_set():
                        raise DownloadCancelled("Cancelled")
                check()
                os.makedirs(WORK, exist_ok=True)
                job["staging"] = staging = tempfile.mkdtemp(dir=WORK)
                opts, st = build_options(job, d, staging), {"file": None, "stage": -1}

                def on_progress(s):
                    check()
                    if s.get("status") == "downloading":
                        if s.get("filename") != st["file"]:
                            st["file"], st["stage"] = s.get("filename"), st["stage"] + 1
                        total = s.get("total_bytes") or s.get("total_bytes_estimate")
                        done = s.get("downloaded_bytes") or 0
                        n = job["stages"]
                        job["percent"] = round(min(99.9, 100 * (min(st["stage"], n - 1) + min(1, done / total)) / n), 1) if total else None
                        job.update(speed=s.get("speed"), eta=s.get("eta"), status="downloading",
                                   message=f"{done/1048576:.1f} MB" + (f" of {total/1048576:.1f} MB" if total else ""))
                    elif s.get("status") == "finished":
                        job.update(status="processing", message="Finishing up", speed=None, eta=None)

                opts["progress_hooks"] = [on_progress]
                opts["postprocessor_hooks"] = [lambda s: (check(), job.update(status="processing", message="Preparing your file", percent=99.9))]
                job.update(status="downloading", message="Connecting")
                with yt_dlp.YoutubeDL(opts) as y:
                    y.download([d["url"]])
                check()
                files = [(n, os.path.getsize(os.path.join(staging, n))) for n in sorted(os.listdir(staging))
                         if not n.endswith((".part", ".ytdl", ".temp", ".jpg", ".png", ".webp"))]
                if not files:
                    raise RuntimeError("The download finished without producing a file.")
                self.finish(job, "completed", "Ready to save", files=files)
        except Exception as e:
            if job["cancel"].is_set() or isinstance(e, DownloadCancelled):
                self.finish(job, "cancelled", "Cancelled", percent=0)
                if job["staging"]:
                    shutil.rmtree(job["staging"], ignore_errors=True)
            else:
                self.finish(job, "error", "Download failed", error=clean_error(e))


jobs, searches = Jobs(), {}


def do_search(rec, url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": Live(rec), "noplaylist": True,
            "extract_flat": "in_playlist", "playlistend": 200}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    rec.update(message="Building the format list", percent=95)
    if info.get("_type") == "playlist":
        ents = [{"url": e.get("url") or e.get("webpage_url"), "title": e.get("title") or "Untitled", "duration": fmt_duration(e.get("duration"))}
                for e in info.get("entries") or [] if e and str(e.get("url") or e.get("webpage_url") or "").startswith("http")]
        return {"type": "playlist", "title": info.get("title") or "Playlist", "uploader": info.get("uploader") or info.get("channel") or "",
                "count": len(ents), "entries": ents}
    fmts, vids, auds = info.get("formats") or [], [], []
    sz = lambda f: f.get("filesize") or f.get("filesize_approx")
    best_audio = max([sz(f) or 0 for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")] or [0])
    for f in fmts:
        has_v, has_a = f.get("vcodec") not in (None, "none"), f.get("acodec") not in (None, "none")
        if not has_v and not has_a:
            continue
        own = sz(f)
        row = {"id": str(f.get("format_id") or ""), "ext": f.get("ext") or "?", "size": own,
               "approx": bool(own and not f.get("filesize")), "note": f.get("format_note") or ""}
        if has_v:
            h = f.get("height") or 0
            row.update(label=f"{h}p" if h else (row["note"] or row["id"]), height=h, width=f.get("width") or 0,
                       vcodec=(f.get("vcodec") or "-").split(".")[0], fps=f.get("fps"), audio=has_a,
                       size=(own + (0 if has_a else best_audio)) if own else None)
            vids.append(row)
        else:
            row.update(codec=(f.get("acodec") or "-").split(".")[0], abr=round(f.get("abr") or 0))
            auds.append(row)
    vids.sort(key=lambda r: (r["height"], r["audio"], r["fps"] or 0, r["size"] or 0), reverse=True)
    auds.sort(key=lambda r: (r["abr"], r["size"] or 0), reverse=True)
    return {"type": "video", "title": info.get("title") or "Video", "thumb": info.get("thumbnail") or "",
            "uploader": info.get("uploader") or info.get("channel") or "Unknown", "duration": fmt_duration(info.get("duration")),
            "site": info.get("extractor_key") or "", "formats": vids, "audio_formats": auds,
            "heights": sorted({r["height"] for r in vids if r["height"]}, reverse=True)}


def run_search(rec, url):
    try:
        rec.update(result=do_search(rec, url), status="done", message="Done", percent=100)
    except Exception as e:
        rec.update(status="error", error=clean_error(e))


atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def guard(self, api):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            raise PermissionError("Unexpected host.")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
            raise PermissionError("Unexpected origin.")
        if api:
            if self.headers.get("X-Token") != TOKEN:
                raise PermissionError("Invalid token. Reload the page.")
            if self.command == "POST" and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise PermissionError("JSON requests only.")

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 100_000:
                raise ValueError("Request is too large.")
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON.")
        if not isinstance(data, dict):
            raise ValueError("Invalid request.")
        return data

    def serve_file(self, path):
        _, jid, dl, idx = path.split("/")[1:5]
        job = jobs.items.get(jid)
        if not job or job["dl"] != dl or not idx.isdigit() or int(idx) >= len(job["files"]):
            return self.send_json({"error": "File expired. Download it again."}, 404)
        name, size = job["files"][int(idx)]
        with open(os.path.join(job["staging"], name), "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def handle_request(self, method):
        path = urlparse(self.path).path
        try:
            self.guard(path.startswith("/api/"))
            if method == "GET":
                if path == "/":
                    raw = open(os.path.join(HERE, "index.html"), encoding="utf-8").read().replace("{{TOKEN}}", TOKEN).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return self.wfile.write(raw)
                if path.startswith("/file/"):
                    return self.serve_file(path)
                if path == "/api/info":
                    return self.send_json({"ytdlp": yt_dlp.version.__version__, "ffmpeg": bool(FFMPEG)})
                if path == "/api/jobs":
                    return self.send_json({"jobs": jobs.listing()})
                if path.startswith("/api/search/"):
                    rec = searches.get(path.rsplit("/", 1)[-1])
                    return self.send_json({k: v for k, v in rec.items()} if rec else {"error": "Unknown search"}, 200 if rec else 404)
                return self.send_json({"error": "Not found"}, 404)
            data = self.read_json()
            url = str(data.get("url", "")).strip()
            if path == "/api/search":
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Enter a link that starts with http:// or https://")
                rec = {"status": "running", "message": "Starting", "percent": 5, "error": "", "result": None}
                sid = uuid.uuid4().hex
                searches[sid] = rec
                threading.Thread(target=run_search, args=(rec, url), daemon=True).start()
                return self.send_json({"id": sid})
            if path == "/api/download":
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Search for a valid link first.")
                return self.send_json({"id": jobs.create({**data, "url": url})})
            if path.startswith("/api/cancel/"):
                ok = jobs.cancel(path.rsplit("/", 1)[-1])
                return self.send_json({"cancelled": ok}, 200 if ok else 409)
            if path == "/api/clear":
                jobs.clear()
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except PermissionError as e:
            self.send_json({"error": str(e)}, 403)
        except ValueError as e:
            self.send_json({"error": clean_error(e)}, 400)
        except Exception as e:
            self.send_json({"error": clean_error(e)}, 500)

    do_GET = lambda self: self.handle_request("GET")
    do_POST = lambda self: self.handle_request("POST")


def main():
    server, port = None, 8765
    for port in range(8765, 8785):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        raise SystemExit("No free port between 8765 and 8784.")
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"Stypzy Video running at http://127.0.0.1:{port}  (FFmpeg: {'yes' if FFMPEG else 'no'})", flush=True)
    threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for j in list(jobs.items.values()):
            j["cancel"].set()
        server.server_close()


if __name__ == "__main__":
    main()