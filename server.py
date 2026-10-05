"""Stypzy Video v2 - run: python server.py   (needs: python -m pip install -U yt-dlp)
Files are prepared in a temp folder, then handed to the browser's own download flow."""
import atexit, hashlib, hmac, json, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, traceback, urllib.request, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, parse_qs
from http.cookies import SimpleCookie

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "stypzy-error.log")


def log_crash(note=""):
    """Print the traceback and keep a copy next to server.py so the reason is never lost."""
    text = traceback.format_exc()
    print("\n" + (note + "\n" if note else "") + text, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {note}\n{text}\n")
    except OSError:
        pass


def pause():
    """Keep the console open so error text can be read (skipped when start.bat is the launcher)."""
    if not os.environ.get("STYPZY_NOPAUSE") and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass


def fatal(msg):
    """Setup problem that restarting cannot fix: show it, wait, exit with code 2."""
    print("\n" + msg, flush=True)
    pause()
    sys.exit(2)


try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, DownloadError
except ImportError:
    fatal("yt-dlp is required for the Python you are running:\n  " + sys.executable +
          "\nInstall it with:\n  \"" + sys.executable + "\" -m pip install -U yt-dlp")

WORK = os.path.join(tempfile.gettempdir(), "stypzy-video")
TOKEN = secrets.token_urlsafe(24)
FFMPEG = shutil.which("ffmpeg") or (
    os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe") if os.name == "nt" else None)
if FFMPEG and not os.path.isfile(FFMPEG):
    FFMPEG = None
if not FFMPEG:
    # No system ffmpeg (e.g. on Render): use the copy that pip installs with imageio-ffmpeg.
    try:
        import imageio_ffmpeg
        _exe = imageio_ffmpeg.get_ffmpeg_exe()
        _bin = os.path.join(tempfile.gettempdir(), "stypzy-bin")
        os.makedirs(_bin, exist_ok=True)
        FFMPEG = os.path.join(_bin, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")   # yt-dlp looks for this exact name
        if not os.path.exists(FFMPEG):
            shutil.copy2(_exe, FFMPEG)
        os.chmod(FFMPEG, 0o755)
    except Exception:
        FFMPEG = None
ARIA2 = None if os.environ.get("STYPZY_NO_ARIA") else (shutil.which("aria2c") or (
    os.path.join(os.path.dirname(sys.executable), "aria2c.exe") if os.name == "nt" else None))
if ARIA2 and not os.path.isfile(ARIA2):
    ARIA2 = None
SLOTS, ALLOWED_HOSTS, TERMINAL = threading.Semaphore(2), set(), ("completed", "cancelled", "error")
TTL = 1800  # seconds a finished file stays available
BROWSERS = ("chrome", "edge", "firefox", "brave", "opera", "chromium")
SETTINGS, LATEST = {"browser": "", "cookies": False}, {"v": None}
COOKIE_FILE = os.path.join(WORK, "cookies.txt")
LAN = "--lan" in sys.argv     # opt-in: let other devices on your network use this server
LOCAL_ONLY = ("/api/settings", "/api/update")   # only the host computer may change these

# Hosted mode (Render sets RENDER=true; or set STYPZY_PUBLIC=1): password-protected, with limits.
PUBLIC = bool(os.environ.get("RENDER") or os.environ.get("STYPZY_PUBLIC"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PROXY = os.environ.get("YTDLP_PROXY", "")                                  # optional: route yt-dlp through a proxy
MAX_MB = int(os.environ.get("MAX_MB", "400")) if PUBLIC else 0              # largest file a visitor may fetch
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE", "2")) if PUBLIC else 99       # active downloads per visitor
if PUBLIC:
    SLOTS = threading.Semaphore(int(os.environ.get("MAX_JOBS", "1")))       # free instances are tiny: 1 at a time
    TTL = 900
    if not APP_PASSWORD:
        print("NOTE: no APP_PASSWORD set, so anyone with the link can use this server.", flush=True)
AUTH_COOKIE = hmac.new(APP_PASSWORD.encode(), b"stypzy-auth", hashlib.sha256).hexdigest() if APP_PASSWORD else ""
FAILS = {}   # ip -> (failed logins, first failure time)

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Stypzy Video - Sign in</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070812;color:#f1f2fb;font:15px system-ui,sans-serif}
form{background:#ffffff0a;border:1px solid #ffffff1f;border-radius:20px;padding:28px;width:min(92vw,340px);display:grid;gap:14px}
h1{margin:0;font-size:22px}input,button{font:inherit;border-radius:12px;border:1px solid #ffffff1f;padding:12px 14px;color:inherit;background:#00000040}
button{border:0;background:linear-gradient(110deg,#8b5cf6,#22d3ee);color:#fff;font-weight:700;cursor:pointer}.e{color:#fb7185;min-height:1em;font-size:13px}</style></head>
<body><form method="post" action="/login"><h1>Stypzy Video</h1><input type="password" name="password" placeholder="Password" autofocus required><div class="e">{{ERR}}</div><button>Sign in</button></form></body></html>"""


def lan_hosts(port):
    """Names/IPs other devices may use to reach this server."""
    names = {socket.gethostname().lower()}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no traffic is sent; just picks the LAN interface
            ip = s.getsockname()[0]
            names.add(ip)
    except OSError:
        ip = None
    return ip, {f"{n}:{port}" for n in names}


class StopRec(Exception):
    """Raised to end a live recording but keep what was captured."""


def apply_auth(opts):
    """Use the person's own login (cookies) so they can reach content their account can already watch."""
    if PROXY:
        opts["proxy"] = PROXY
    if PUBLIC:
        return opts   # never use a shared login on a public server
    if SETTINGS["cookies"] and os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    elif SETTINGS["browser"]:
        opts["cookiesfrombrowser"] = (SETTINGS["browser"],)
    return opts


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def check_latest():
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=8) as r:
            LATEST["v"] = json.load(r)["info"]["version"]
    except Exception:
        pass


def salvage(staging):
    """Turn the partial live recording into a playable file."""
    parts = [n for n in os.listdir(staging) if n.endswith(".part")]
    if not parts:
        raise RuntimeError("Nothing was recorded yet. Let it run a little longer before stopping.")
    src = os.path.join(staging, parts[0])
    base = os.path.splitext(parts[0][:-5])[0]
    out = os.path.join(staging, base + (".mkv" if FFMPEG else ".ts"))
    if FFMPEG:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-c", "copy", out], timeout=900, check=True)
        os.remove(src)
    else:
        os.replace(src, out)


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
    if mode == "live":
        if PUBLIC:
            raise ValueError("Live recording is turned off on this server.")
        selector, job["stages"] = "b/bv*+ba", 1
    elif mode == "audio":
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
            "retries": 3, "fragment_retries": 3, "continuedl": True,
            "concurrent_fragment_downloads": 8}   # fetch several stream fragments at once (HLS/DASH)
    if re.search(r"(youtube\.com|youtu\.be)", str(d.get("url", ""))):
        opts["http_chunk_size"] = 10 * 1024 * 1024   # ranged chunks avoid YouTube's single-stream throttling
    if MAX_MB:
        opts["max_filesize"] = MAX_MB * 1048576
    if ARIA2 and mode != "live":
        # many parallel connections to the same file: the biggest speed-up on throttled sites
        opts["external_downloader"] = {"default": ARIA2}
        opts["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]}
    if d.get("embed") and FFMPEG:
        opts["writethumbnail"] = True
        pps = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}] + pps + [
            {"key": "FFmpegMetadata", "add_metadata": True}, {"key": "EmbedThumbnail"}]
    if FFMPEG:
        opts.update(ffmpeg_location=os.path.dirname(FFMPEG), merge_output_format="mp4/mkv")
    if mode == "live":
        opts["hls_use_mpegts"] = True
    if pps:
        opts["postprocessors"] = pps
    return apply_auth(opts)


class Jobs:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def create(self, d, owner=""):
        thumb = str(d.get("thumb") or "")
        job = {"id": uuid.uuid4().hex, "dl": secrets.token_urlsafe(16), "title": str(d.get("title") or d["url"])[:200],
               "thumb": thumb if thumb.startswith("http") else "", "label": str(d.get("label") or "")[:60],
               "status": "queued", "percent": 0, "speed": None, "eta": None, "message": "Waiting for a free slot",
               "error": "", "files": [], "created": time.time(), "finished": 0, "stages": 1, "staging": None,
               "cancel": threading.Event(), "stop": threading.Event(), "live": d.get("mode") == "live", "data": d, "owner": owner}
        with self.lock:
            if sum(1 for j in self.items.values() if j["owner"] == owner and j["status"] not in TERMINAL) >= MAX_ACTIVE:
                raise ValueError(f"You already have {MAX_ACTIVE} downloads in progress. Wait for one to finish.")
            self.items[job["id"]] = job
        threading.Thread(target=self.run, args=(job,), daemon=True).start()
        return job["id"]

    def drop(self, k):
        j = self.items.pop(k, None)
        if j and j["staging"]:
            shutil.rmtree(j["staging"], ignore_errors=True)

    def listing(self, owner=None):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL and time.time() - j["finished"] > TTL]:
                self.drop(k)
            return [{**{k: v for k, v in j.items() if k not in ("cancel", "stop", "data", "stages", "staging", "dl", "files", "owner")},
                     "files": [{"name": n, "size": s, "url": f"/file/{j['id']}/{j['dl']}/{i}"} for i, (n, s) in enumerate(j["files"])]}
                    for j in sorted(self.items.values(), key=lambda j: -j["created"]) if owner is None or j["owner"] == owner]

    def finish(self, job, status, message, **extra):
        with self.lock:
            if job["status"] in TERMINAL:
                return
            job.update(status=status, message=message, speed=None, eta=None, finished=time.time(), **extra)
            if status == "completed":
                job["percent"] = 100

    def cancel(self, key, owner=None):
        with self.lock:
            j = self.items.get(key)
            if not j or (owner is not None and j["owner"] != owner) or j["status"] in TERMINAL:
                return False
            j["cancel"].set()
            j["message"] = "Stopping"
            return True

    def stop(self, key, owner=None):
        with self.lock:
            j = self.items.get(key)
            if not j or (owner is not None and j["owner"] != owner) or not j["live"] or j["status"] in TERMINAL:
                return False
            j["stop"].set()
            j["message"] = "Stopping and saving"
            return True

    def clear(self, owner=None):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL and (owner is None or j["owner"] == owner)]:
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
                    if job["stop"].is_set():
                        raise StopRec()
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
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        y.download([d["url"]])
                except StopRec:
                    job.update(status="processing", message="Saving your recording", speed=None, eta=None)
                    salvage(staging)
                check()
                files = [(n, os.path.getsize(os.path.join(staging, n))) for n in sorted(os.listdir(staging))
                         if not n.endswith((".part", ".ytdl", ".temp", ".jpg", ".png", ".webp"))]
                if not files:
                    raise RuntimeError("The download finished without producing a file." + (f" It may be larger than this server's {MAX_MB} MB limit." if MAX_MB else ""))
                self.finish(job, "completed", "Ready to save", files=files)
        except BaseException as e:   # a worker thread must never take the app down
            if job["cancel"].is_set() or isinstance(e, DownloadCancelled):
                self.finish(job, "cancelled", "Cancelled", percent=0)
                if job["staging"]:
                    shutil.rmtree(job["staging"], ignore_errors=True)
            else:
                if not isinstance(e, (DownloadError, ValueError, RuntimeError)):
                    log_crash("Unexpected download error")
                self.finish(job, "error", "Download failed", error=clean_error(e))


jobs, searches = Jobs(), {}


def do_search(rec, url):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": Live(rec), "noplaylist": True,
            "extract_flat": "in_playlist", "playlistend": 25 if PUBLIC else 200}
    with yt_dlp.YoutubeDL(apply_auth(opts)) as y:
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
            "site": info.get("extractor_key") or "", "live": bool(info.get("is_live")), "formats": vids, "audio_formats": auds,
            "heights": sorted({r["height"] for r in vids if r["height"]}, reverse=True)}


def run_search(rec, url):
    try:
        rec.update(result=do_search(rec, url), status="done", message="Done", percent=100)
    except BaseException as e:   # never let a search thread take the app down
        rec.update(status="error", error=clean_error(e))


atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        return c

    def authed(self):
        if not AUTH_COOKIE:
            return True
        m = self.cookies().get("sv_auth")
        return bool(m) and hmac.compare_digest(m.value.encode(), AUTH_COOKIE.encode())

    def sid(self):
        m = self.cookies().get("sv_sid")
        return m.value[:64] if m else "anon"

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if (PUBLIC and fwd) else self.client_address[0]

    def send_html(self, text, status=200):
        raw = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_login(self):
        ip = self.client_ip()
        n, t = FAILS.get(ip, (0, time.time()))
        if time.time() - t > 900:
            n, t = 0, time.time()
        if n >= 8:
            return self.send_html(LOGIN_HTML.replace("{{ERR}}", "Too many attempts. Try again in a few minutes."), 429)
        length = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        pw = (form.get("password") or [""])[0]
        if AUTH_COOKIE and hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            FAILS.pop(ip, None)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"sv_auth={AUTH_COOKIE}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000" + ("; Secure" if PUBLIC else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        FAILS[ip] = (n + 1, t)
        time.sleep(1)
        self.send_html(LOGIN_HTML.replace("{{ERR}}", "Wrong password."), 401)

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
            if n > 3_000_000:
                raise ValueError("Request is too large.")
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON.")
        if not isinstance(data, dict):
            raise ValueError("Invalid request.")
        return data

    def serve_file(self, path):
        _, jid, dl, idx = path.split("/")[1:5]
        with jobs.lock:
            job = jobs.items.get(jid)
            ok = bool(job and job["owner"] == self.sid() and job["dl"] == dl and idx.isdigit() and int(idx) < len(job["files"]))
            if ok:
                name, size = job["files"][int(idx)]
                full = os.path.join(job["staging"], name)
        if not ok:
            return self.send_json({"error": "File expired. Download it again."}, 404)
        try:
            f = open(full, "rb")
        except OSError:
            return self.send_json({"error": "File expired. Download it again."}, 404)
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def handle_request(self, method):
        path = urlparse(self.path).path
        try:
            if path == "/healthz":
                return self.send_json({"ok": True})
            self.guard(path.startswith("/api/"))
            if path in LOCAL_ONLY and (PUBLIC or self.client_address[0] not in ("127.0.0.1", "::1")):
                raise PermissionError("This is turned off on this server." if PUBLIC else "Only the computer running the server can change this.")
            if method == "POST" and path == "/login":
                return self.do_login()
            if path.startswith(("/api/", "/file/")) and not self.authed():
                return self.send_json({"error": "Please sign in again (reload the page)."}, 401)
            if method == "GET":
                if path == "/":
                    if not self.authed():
                        return self.send_html(LOGIN_HTML.replace("{{ERR}}", ""))
                    page = open(os.path.join(HERE, "index.html"), encoding="utf-8").read().replace("{{TOKEN}}", TOKEN)
                    if PUBLIC:   # hide the login/update drawer: those features are off on a public server
                        page = page.replace("</head>", "<style>#pSet{display:none!important}</style></head>", 1)
                    raw = page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Frame-Options", "DENY")
                    if "sv_sid" not in self.cookies():
                        self.send_header("Set-Cookie", f"sv_sid={secrets.token_urlsafe(16)}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if PUBLIC else ""))
                    self.end_headers()
                    return self.wfile.write(raw)
                if path.startswith("/file/"):
                    return self.serve_file(path)
                if path == "/api/info":
                    cur, new = yt_dlp.version.__version__, LATEST["v"]
                    return self.send_json({"ytdlp": cur, "latest": new, "update": bool(new and not PUBLIC and vtuple(new) > vtuple(cur)),
                                           "ffmpeg": bool(FFMPEG), **SETTINGS})
                if path == "/api/jobs":
                    return self.send_json({"jobs": jobs.listing(self.sid())})
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
                while len(searches) > 100:          # keep memory bounded
                    searches.pop(next(iter(searches)))
                threading.Thread(target=run_search, args=(rec, url), daemon=True).start()
                return self.send_json({"id": sid})
            if path == "/api/download":
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Search for a valid link first.")
                return self.send_json({"id": jobs.create({**data, "url": url}, self.sid())})
            if path.startswith("/api/cancel/"):
                ok = jobs.cancel(path.rsplit("/", 1)[-1], self.sid())
                return self.send_json({"cancelled": ok}, 200 if ok else 409)
            if path == "/api/settings":
                b = str(data.get("browser", SETTINGS["browser"])).lower()
                if b and b not in BROWSERS:
                    raise ValueError("Unsupported browser.")
                SETTINGS["browser"] = b
                if data.get("clear_cookies") and os.path.isfile(COOKIE_FILE):
                    os.remove(COOKIE_FILE)
                    SETTINGS["cookies"] = False
                txt = data.get("cookies_text")
                if txt:
                    if "\t" not in str(txt):
                        raise ValueError("That doesn't look like a cookies.txt file (Netscape format).")
                    os.makedirs(WORK, exist_ok=True)
                    fd = os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(str(txt))
                    SETTINGS["cookies"] = True
                return self.send_json(dict(SETTINGS))
            if path.startswith("/api/stop/"):
                ok = jobs.stop(path.rsplit("/", 1)[-1], self.sid())
                return self.send_json({"stopped": ok}, 200 if ok else 409)
            if path == "/api/update":
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], capture_output=True, text=True, timeout=300)
                if r.returncode:
                    raise RuntimeError(clean_error(r.stderr or r.stdout))
                return self.send_json({"ok": True, "output": clean_error(r.stdout)})
            if path == "/api/clear":
                jobs.clear(self.sid())
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except (ConnectionError, TimeoutError):
            return   # the browser closed the connection (e.g. cancelled a download); nothing to report
        except PermissionError as e:
            self.safe_error(e, 403)
        except ValueError as e:
            self.safe_error(e, 400)
        except Exception as e:
            log_crash(f"Unhandled error on {method} {path}")
            self.safe_error(e, 500)

    def safe_error(self, e, status):
        try:
            self.send_json({"error": clean_error(e)}, status)
        except Exception:
            pass

    do_GET = lambda self: self.handle_request("GET")
    do_POST = lambda self: self.handle_request("POST")


def main():
    server, port = None, 0
    ports = [int(os.environ["PORT"])] if (PUBLIC and os.environ.get("PORT")) else range(8765, 8785)
    for port in ports:
        try:
            server = ThreadingHTTPServer(("0.0.0.0" if (LAN or PUBLIC) else "127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        fatal("No free port between 8765 and 8784. Close other copies of this app and try again.")
    server.daemon_threads = True
    ALLOWED_HOSTS.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    lan_ip = None
    if PUBLIC:
        public_names = [h.strip().lower() for h in [os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")] + os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
        ALLOWED_HOSTS.update(public_names)
        if not public_names:
            print("WARNING: no public hostname known. Set ALLOWED_HOSTS to your site's domain.", flush=True)
    if LAN:
        lan_ip, extra = lan_hosts(port)
        ALLOWED_HOSTS.update(extra)
    shutil.rmtree(WORK, ignore_errors=True)
    threading.Thread(target=check_latest, daemon=True).start()
    print(f"Stypzy Video running at http://127.0.0.1:{port}  (FFmpeg: {'yes' if FFMPEG else 'no'}, aria2c: {'yes' if ARIA2 else 'no'})", flush=True)
    if LAN:
        print(f"\nNETWORK MODE: other devices on your Wi-Fi/LAN can open  http://{lan_ip or '<this-pc-ip>'}:{port}", flush=True)
        print("Anyone on your network can use the page and see the shared download list. Only use trusted networks.", flush=True)
    print("Keep this window open while you use the page. Press Ctrl+C to stop.", flush=True)
    if not PUBLIC:
        threading.Timer(0.7, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        while True:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopping...")
                break
            except Exception:
                log_crash("Server loop error - continuing")
                time.sleep(1)
    finally:
        for j in list(jobs.items.values()):
            j["cancel"].set()
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        log_crash("Fatal error")
        pause()
        sys.exit(1)