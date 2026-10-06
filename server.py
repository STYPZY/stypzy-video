"""Stypzy Video v2 - run: python server.py   (needs: python -m pip install -U yt-dlp)
Files are prepared in a temp folder, then streamed to the browser's download flow."""
import atexit, hashlib, hmac, ipaddress, json, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time, traceback, urllib.request, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, parse_qs
from http.cookies import SimpleCookie

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "stypzy-error.log")


def log_crash(note=""):
    """Keep diagnostic stack frames without saving exception text that may contain a URL."""
    exc_type, _, tb = sys.exc_info()
    text = "".join(traceback.format_tb(tb))
    if exc_type:
        text += f"{exc_type.__name__}: [details omitted for privacy]\n"
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
SLOTS, ALLOWED_HOSTS, TERMINAL = threading.Semaphore(2), set(), ("ready", "completed", "cancelled", "error")
TTL = 1800  # seconds a finished file stays available
BROWSERS = ("chrome", "edge", "firefox", "brave", "opera", "chromium")
LATEST, SESS, SESS_LOCK = {"v": None}, {}, threading.Lock()   # per-tab login settings
ENDED_TABS, SEARCH_LOCK = {}, threading.Lock()
PENDING_DELETE, DELETE_LOCK = set(), threading.Lock()
LAN = "--lan" in sys.argv     # opt-in: let other devices on your network use this server
LOCAL_ONLY = ("/api/update", "/api/login/test")   # only the computer running the app may use these

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
IDLE = 900 if PUBLIC else 300      # fallback cleanup if the browser cannot notify us that its tab closed

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


def cookie_path(tab):
    return os.path.join(WORK, "ck-" + hashlib.sha256(tab.encode()).hexdigest()[:24] + ".txt")


def remove_path(path, retry=True):
    """Remove app-owned temporary data, retrying paths that Windows still has open."""
    if not path:
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        if retry:
            with DELETE_LOCK:
                PENDING_DELETE.add(path)


def clear_yt_dlp_cookies(ydl):
    """Drop yt-dlp's in-memory cookie jar as soon as its operation is done."""
    if ydl is None:
        return
    jar = getattr(ydl, "cookiejar", None)
    if jar is not None:
        try:
            jar.clear()
        except Exception:
            pass


def touch(tab):
    """Mark a browser tab as active and return its private login settings."""
    if not tab:
        return None
    with SESS_LOCK:
        if tab in ENDED_TABS:
            return None
        s = SESS.setdefault(tab, {"method": "none", "browser": "", "cookies": False, "domains": [], "count": 0})
        s["seen"], s["close_at"] = time.time(), 0
        return s


def public_state(tab):
    s = SESS.get(tab) or {}
    return {k: s.get(k, d) for k, d in (("method", "none"), ("browser", ""), ("cookies", False), ("domains", []), ("count", 0))}


def wipe_tab(tab):
    """Expire one page session and remove its app-owned files and metadata."""
    if not tab:
        return
    with SESS_LOCK:
        ENDED_TABS[tab] = time.time()
        SESS.pop(tab, None)
    purge_tab_data(tab)


def purge_tab_data(tab):
    """Clear the resources owned by a tab after its session has been expired."""
    jobs.wipe_tab(tab)
    with SEARCH_LOCK:
        for key, entry in list(searches.items()):
            if entry["tab"] == tab:
                entry["cancel"].set()
                entry["record"].clear()
                searches.pop(key, None)
    remove_path(cookie_path(tab))
    # Remove older diagnostic logs that might contain a URL from a prior version.
    remove_path(LOG)


def expire_if_idle(tab, now):
    with SESS_LOCK:
        s = SESS.get(tab)
        if not s:
            return
        closing = s.get("close_at") and now > s["close_at"]
        idle = now - s.get("seen", now) > IDLE
        if not (closing or idle):
            return
        ENDED_TABS[tab] = now
        SESS.pop(tab, None)
    purge_tab_data(tab)


def reaper():
    while True:
        time.sleep(5)
        now = time.time()
        with SESS_LOCK:
            tabs = list(SESS)
        for tab in tabs:
            expire_if_idle(tab, now)
        with SESS_LOCK:
            for tab, ended_at in list(ENDED_TABS.items()):
                if now - ended_at > 86400:
                    ENDED_TABS.pop(tab, None)
        with DELETE_LOCK:
            retrying = list(PENDING_DELETE)
            PENDING_DELETE.clear()
        for path in retrying:
            remove_path(path)


def apply_auth(opts, tab=""):
    """Use this tab's own login so it can reach content its account can already watch. Never shared between tabs."""
    if PROXY:
        opts["proxy"] = PROXY
    s = SESS.get(tab)
    if not s:
        return opts
    if s["method"] == "file" and s["cookies"] and os.path.isfile(cookie_path(tab)):
        opts["cookiefile"] = cookie_path(tab)
    elif s["method"] == "browser" and s["browser"] and not PUBLIC:
        opts["cookiesfrombrowser"] = (s["browser"],)
    return opts


def has_auth(tab):
    with SESS_LOCK:
        s = SESS.get(tab) or {}
        return bool((s.get("method") == "file" and s.get("cookies") and os.path.isfile(cookie_path(tab)))
                    or (s.get("method") == "browser" and s.get("browser") and not PUBLIC))


def parse_cookies(txt):
    """Normalize a Netscape cookies.txt. Returns (text, cookie count, top domains)."""
    if len(txt) > 1_500_000 or "\x00" in txt:
        raise ValueError("That file is too large or isn't a text file.")
    txt = txt.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = txt.split("\n")
    n, doms = 0, {}
    for line in lines:
        marker = line.lstrip()
        if not line.strip() or (marker.startswith("#") and not marker.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError("That isn't a Netscape-format cookies.txt (each line needs 7 tab-separated fields).")
        d = parts[0].replace("#HttpOnly_", "").lstrip(".").lower()
        if (not d or parts[1].upper() not in ("TRUE", "FALSE") or not parts[2].startswith("/")
                or parts[3].upper() not in ("TRUE", "FALSE") or not re.fullmatch(r"-?\d+", parts[4]) or not parts[5]):
            raise ValueError("That cookies.txt contains an invalid Netscape cookie row. Export it again in Netscape format.")
        doms[d], n = doms.get(d, 0) + 1, n + 1
    if not n:
        raise ValueError("No cookies found in that file.")
    header = lines[0].strip().lower() if lines else ""
    if header not in ("# http cookie file", "# netscape http cookie file"):
        lines.insert(0, "# Netscape HTTP Cookie File")
    return "\n".join(lines).rstrip("\n") + "\n", n, sorted(doms, key=doms.get, reverse=True)[:6]


def validate_cookie_file(path):
    """Ask yt-dlp's own cookie loader to parse the normalized file before accepting it."""
    from yt_dlp.cookies import load_cookies
    ydl, jar = None, None
    try:
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": Silent()})
        with ydl:
            jar = load_cookies(path, None, ydl)
            return sum(1 for _ in jar)
    except Exception as e:
        raise ValueError("yt-dlp couldn't read this cookies.txt. Export it in Netscape format and try again.") from e
    finally:
        if jar is not None:
            try:
                jar.clear()
            except Exception:
                pass
        clear_yt_dlp_cookies(ydl)


def test_browser_login(browser):
    from yt_dlp.cookies import extract_cookies_from_browser
    jar = extract_cookies_from_browser(browser, None, Silent())
    try:
        doms = {}
        for c in jar:
            d = (c.domain or "").lstrip(".")
            doms[d] = doms.get(d, 0) + 1
        if not doms:
            raise ValueError("That browser has no saved logins yet. Sign in to the site in that browser first.")
        return sum(doms.values()), sorted(doms, key=doms.get, reverse=True)[:6]
    finally:
        try:
            jar.clear()
        except Exception:
            pass


def check_url(url):
    """On shared servers, refuse links that point at private networks (SSRF protection)."""
    if not (PUBLIC or LAN):
        return
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError("Couldn't find that website. Check the link.")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("That address points to a private network and is blocked on this server.")


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
    out = []
    for l in str(e).splitlines():
        l = re.sub(r"^(ERROR:\s*)+", "", re.sub(r"\x1b\[[0-9;]*m", "", l).strip())   # yt-dlp prints "ERROR: ERROR:" twice
        if l and (not out or out[-1] != l):
            out.append(l)
    return "\n".join(out[-3:]) or "Something went wrong."


FRIENDLY = (
    (("could not copy",), "login", "Your browser is still open, so Windows has locked its saved logins. Fully close that browser (also from the system tray), or choose Firefox, or upload a cookies.txt file instead."),
    (("failed to decrypt", "dpapi", "app-bound", "v20 cookie"), "login", "This browser encrypts its cookies in a way this app cannot read (newer Chrome, Edge and Brave). Choose Firefox or upload a cookies.txt file instead."),
    (("cookies database", "cookie database"), "login", "Couldn't find or open that browser's saved logins. Pick the browser you actually signed in with, or upload a cookies.txt file."),
    (("login required", "sign in", "log in", "logged in", "private video", "confirm your age", "age-restricted", "members-only", "rate-limit reached", "empty media response", "--cookies"), "login", "This link needs you to be signed in. Open Login and updates and use a login from an account that is allowed to watch it."),
    (("unsupported url", "no video formats"), "", "This link isn't supported or has no downloadable video."),
    (("requested format is not available",), "", "That format isn't available for this video. Pick a different one."),
    (("http error 403", "http error 429", "too many requests", "unable to extract", "nsig", "signature extraction", "sabr"), "update", "The site refused the request or changed how it works. Updating yt-dlp usually fixes this."),
)


def friendly(e, tab=""):
    """Plain-English message plus which settings fix to offer ('login', 'update' or '')."""
    msg = clean_error(e)
    low = msg.lower()
    if any(term in low for term in ("netscape format", "failed to load cookies", "couldn't read cookies")):
        return "yt-dlp couldn't read the cookies.txt file. Export it again in Netscape format and try again.", "login"
    s = SESS.get(tab) if tab else None
    auth_active = bool(s and (
        (s.get("method") == "file" and s.get("cookies") and os.path.isfile(cookie_path(tab)))
        or (s.get("method") == "browser" and s.get("browser") and not PUBLIC)))
    if auth_active and any(term in low for term in ("http error 401", "http error 403", "forbidden", "login required", "sign in", "not logged in")):
        return ("The site rejected access. Check that this browser or cookies.txt is signed in to this exact site, then refresh the login and try again. Some sites also block downloader access.", "login")
    for needles, fix, text in FRIENDLY:
        if any(n in low for n in needles):
            return text, fix
    return msg, ""


def generic_retry_worthwhile(e):
    """Whether a failed site extractor might be recoverable from the page/embed itself."""
    low = str(e).lower()
    return any(term in low for term in ("unsupported url", "no video formats", "unable to extract",
                                         "failed to extract", "could not extract"))


def fmt_duration(sec):
    if not sec:
        return None
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Silent:
    debug = info = warning = error = lambda self, *a, **k: None


class Live:
    """Turns yt-dlp's log lines into friendly search status."""
    STEPS = (("Extracting URL", "Connecting to the site"), ("webpage", "Reading the page"), ("player", "Loading player data"),
             ("manifest", "Reading stream lists"), ("m3u8", "Reading stream lists"), ("Downloading", "Collecting formats"))

    def __init__(self, rec, cancel=None):
        self.rec, self.n, self.cancel = rec, 0, cancel

    def debug(self, msg):
        for key, text in self.STEPS:
            if key in msg:
                self.n += 1
                with SEARCH_LOCK:
                    if self.cancel and self.cancel.is_set():
                        return
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
    if d.get("force_generic"):
        opts["force_generic_extractor"] = True
    if re.search(r"(youtube\.com|youtu\.be)", str(d.get("url", ""))):
        opts["http_chunk_size"] = 10 * 1024 * 1024   # ranged chunks avoid YouTube's single-stream throttling
    if MAX_MB:
        opts["max_filesize"] = MAX_MB * 1048576
    # Avoid passing authenticated request headers to an external downloader process.
    if ARIA2 and mode != "live" and not has_auth(job.get("tab", "")):
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
    return apply_auth(opts, job.get("tab", ""))


class Jobs:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def create(self, d, owner=""):
        tab = owner.partition(":")[2]
        thumb = str(d.get("thumb") or "")
        job = {"id": uuid.uuid4().hex, "dl": secrets.token_urlsafe(16), "title": str(d.get("title") or d["url"])[:200],
               "thumb": thumb if thumb.startswith("http") else "", "label": str(d.get("label") or "")[:60],
               "status": "queued", "percent": 0, "speed": None, "eta": None, "message": "Waiting for a free slot",
               "error": "", "files": [], "created": time.time(), "finished": 0, "stages": 1, "staging": None,
               "delivery_total": 0, "delivery_sent": 0, "delivery_by_file": {}, "delivery_done": set(), "delivery_active": set(),
               "cancel": threading.Event(), "stop": threading.Event(), "purged": False, "live": d.get("mode") == "live", "data": d, "owner": owner, "tab": tab, "fix": ""}
        with SESS_LOCK:
            if tab in ENDED_TABS:
                raise PermissionError("This page session has ended. Reload the app to start a fresh session.")
            with self.lock:
                if sum(1 for j in self.items.values() if j["owner"] == owner and j["status"] not in TERMINAL) >= MAX_ACTIVE:
                    raise ValueError(f"You already have {MAX_ACTIVE} downloads in progress. Wait for one to finish.")
                self.items[job["id"]] = job
        threading.Thread(target=self.run, args=(job,), daemon=True).start()
        return job["id"]

    def drop(self, k):
        j = self.items.pop(k, None)
        if not j:
            return
        j["purged"] = True
        if j["status"] not in TERMINAL:
            j["cancel"].set()
            j["stop"].set()
        remove_path(j.get("staging"))
        j["staging"] = None
        j["files"].clear()
        if isinstance(j.get("data"), dict):
            j["data"].clear()
        j.update(title="", thumb="", label="", error="", message="Removed")

    def listing(self, owner=None):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL and time.time() - j["finished"] > TTL]:
                self.drop(k)
            return [{**{k: v for k, v in j.items() if k not in ("cancel", "stop", "data", "stages", "staging", "dl", "files", "owner", "tab", "delivery_by_file", "delivery_done", "delivery_active")},
                     "files": [{"name": n, "size": s, "url": f"/file/{j['id']}/{j['dl']}/{i}?tab={quote(j['tab'])}"} for i, (n, s) in enumerate(j["files"])]}
                    for j in sorted(self.items.values(), key=lambda j: -j["created"]) if owner is None or j["owner"] == owner]

    def finish(self, job, status, message, **extra):
        with self.lock:
            if job["status"] in TERMINAL:
                return
            job.update(status=status, message=message, speed=None, eta=None, finished=time.time(), **extra)
            if status == "completed":
                job["percent"] = 100
            elif status == "ready":
                job["percent"] = 50

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

    def wipe_tab(self, tab):
        if not tab:
            return
        with self.lock:
            for k in [k for k, j in self.items.items() if j["tab"] == tab]:
                self.drop(k)

    def clear(self, owner=None):
        with self.lock:
            for k in [k for k, j in self.items.items() if j["status"] in TERMINAL and (owner is None or j["owner"] == owner)]:
                self.drop(k)

    def run(self, job):
        d = job["data"]
        staging = None
        try:
            with SLOTS:
                def check():
                    if job["cancel"].is_set():
                        raise DownloadCancelled("Cancelled")
                check()
                os.makedirs(WORK, exist_ok=True)
                job["staging"] = staging = tempfile.mkdtemp(dir=WORK)
                check()
                opts, st = build_options(job, d, staging), {"file": None, "stage": -1}
                check()

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
                        # The first half represents fetching/preparing on the server.
                        # The second half is reserved for sending the bytes to the browser.
                        job["percent"] = round(min(49.9, 50 * (min(st["stage"], n - 1) + min(1, done / total)) / n), 1) if total else None
                        job.update(speed=s.get("speed"), eta=s.get("eta"), status="downloading",
                                   message=f"{done/1048576:.1f} MB" + (f" of {total/1048576:.1f} MB" if total else ""))
                    elif s.get("status") == "finished":
                        job.update(status="processing", message="Finishing up", speed=None, eta=None, percent=49.9)

                opts["progress_hooks"] = [on_progress]
                opts["postprocessor_hooks"] = [lambda s: (check(), job.update(status="processing", message="Preparing your file", percent=49.9))]
                job.update(status="downloading", message="Connecting")
                ydl = None
                try:
                    ydl = yt_dlp.YoutubeDL(opts)
                    with ydl:
                        ydl.download([d["url"]])
                except StopRec:
                    job.update(status="processing", message="Saving your recording", speed=None, eta=None)
                    salvage(staging)
                finally:
                    clear_yt_dlp_cookies(ydl)
                check()
                files = [(n, os.path.getsize(os.path.join(staging, n))) for n in sorted(os.listdir(staging))
                         if not n.endswith((".part", ".ytdl", ".temp", ".jpg", ".png", ".webp"))]
                if not files:
                    raise RuntimeError("The download finished without producing a file." + (f" It may be larger than this server's {MAX_MB} MB limit." if MAX_MB else ""))
                job.update(delivery_total=sum(size for _, size in files), delivery_sent=0,
                           delivery_by_file={}, delivery_done=set(), delivery_active=set())
                self.finish(job, "ready", "Starting browser download", files=files)
        except BaseException as e:   # a worker thread must never take the app down
            if job["cancel"].is_set() or isinstance(e, DownloadCancelled):
                self.finish(job, "cancelled", "Cancelled", percent=0)
                if job["staging"]:
                    remove_path(job["staging"])
                    job["staging"] = None
            else:
                if not isinstance(e, (DownloadError, ValueError, RuntimeError)):
                    log_crash("Unexpected download error")
                msg, fix = friendly(e, job.get("tab", ""))
                self.finish(job, "error", "Download failed", error=msg, fix=fix)
        finally:
            # The original link/options are no longer needed after an attempt finishes.
            if isinstance(d, dict):
                d.clear()
            job["data"] = {}
            if job.get("purged") or job["cancel"].is_set():
                remove_path(staging)
                job["staging"] = None
                job["files"].clear()


jobs, searches = Jobs(), {}


class SearchCancelled(Exception):
    pass


def extract_with_auth(url, opts, tab=""):
    ydl = None
    try:
        ydl = yt_dlp.YoutubeDL(apply_auth(opts, tab))
        with ydl:
            return ydl.extract_info(url, download=False)
    finally:
        clear_yt_dlp_cookies(ydl)


def do_search(rec, url, tab="", cancel=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": Live(rec, cancel), "noplaylist": True,
            "extract_flat": "in_playlist", "playlistend": 25 if PUBLIC else 200}
    if cancel and cancel.is_set():
        raise SearchCancelled()
    used_generic = False
    try:
        info = extract_with_auth(url, opts, tab)
    except Exception as e:
        if cancel and cancel.is_set():
            raise SearchCancelled() from e
        if not generic_retry_worthwhile(e):
            raise
        generic_opts = {**opts, "force_generic_extractor": True}
        info = extract_with_auth(url, generic_opts, tab)
        used_generic = True
    if cancel and cancel.is_set():
        raise SearchCancelled()
    if info.get("_type") != "playlist" and not info.get("formats") and not used_generic:
        generic_opts = {**opts, "force_generic_extractor": True}
        info = extract_with_auth(url, generic_opts, tab)
        used_generic = True
    if cancel and cancel.is_set():
        raise SearchCancelled()
    with SEARCH_LOCK:
        if cancel and cancel.is_set():
            raise SearchCancelled()
        rec.update(message="Building the format list", percent=95)
    if info.get("_type") == "playlist":
        ents = [{"url": e.get("url") or e.get("webpage_url"), "title": e.get("title") or "Untitled", "duration": fmt_duration(e.get("duration"))}
                for e in info.get("entries") or [] if e and str(e.get("url") or e.get("webpage_url") or "").startswith("http")]
        return {"type": "playlist", "title": info.get("title") or "Playlist", "uploader": info.get("uploader") or info.get("channel") or "",
                "count": len(ents), "entries": ents, "force_generic": used_generic}
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
            "heights": sorted({r["height"] for r in vids if r["height"]}, reverse=True), "force_generic": used_generic}


def run_search(rec, url, tab="", cancel=None):
    try:
        result = do_search(rec, url, tab, cancel)
        with SEARCH_LOCK:
            if cancel and cancel.is_set():
                return
            rec.update(result=result, status="done", message="Done", percent=100)
    except SearchCancelled:
        return
    except BaseException as e:   # never let a search thread take the app down
        msg, fix = friendly(e, tab)
        with SEARCH_LOCK:
            if cancel and cancel.is_set():
                return
            rec.update(status="error", error=msg, fix=fix)


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
        return (re.sub(r"[^\w-]", "", m.value)[:64] or "anon") if m else "anon"

    def tab(self):
        t = self.headers.get("X-Tab", "")
        return t if re.fullmatch(r"[A-Za-z0-9_-]{8,40}", t) else ""

    def owner(self):
        return f"{self.sid()}:{self.tab()}"

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def nocache(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if (PUBLIC and fwd) else self.client_address[0]   # last hop = the proxy-verified one

    def send_html(self, text, status=200):
        raw = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.nocache()
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
        self.nocache()
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
        tab = (parse_qs(urlparse(self.path).query).get("tab") or [""])[0]
        with SESS_LOCK:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", tab) or tab in ENDED_TABS:
                return self.send_json({"error": "File expired. Download it again."}, 404)
        with jobs.lock:
            job = jobs.items.get(jid)
            ok = bool(job and job["owner"] == f"{self.sid()}:{tab}" and job["dl"] == dl and idx.isdigit() and int(idx) < len(job["files"]))
            if ok:
                file_index = int(idx)
                name, size = job["files"][file_index]
                full = os.path.join(job["staging"], name)
                track = job["status"] in ("ready", "sending") and file_index not in job["delivery_done"]
                if track and file_index in job["delivery_active"]:
                    return self.send_json({"error": "This file is already being sent to your browser."}, 409)
                if track:
                    job["delivery_active"].add(file_index)
                    job["delivery_by_file"][file_index] = 0
                    job.update(status="sending", message="Sending to your browser", speed=None, eta=None, finished=0)
        if not ok:
            return self.send_json({"error": "File expired. Download it again."}, 404)
        try:
            f = open(full, "rb")
        except OSError:
            if track:
                with jobs.lock:
                    job["delivery_active"].discard(file_index)
                    job["delivery_by_file"].pop(file_index, None)
                    job["delivery_sent"] = sum(job["delivery_by_file"].values())
                    active = bool(job["delivery_active"])
                    pct = 50 + 50 * job["delivery_sent"] / max(1, job["delivery_total"])
                    job.update(status="sending" if active else "ready", percent=round(min(99.9, pct), 1),
                                message="File unavailable. Choose Save to retry." if not active else "Sending to your browser",
                                finished=0 if active else time.time())
            return self.send_json({"error": "File expired. Download it again."}, 404)
        sent, started, transferred = 0, time.monotonic(), False
        with f:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.nocache()
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
                self.end_headers()
                while True:
                    block = f.read(1 << 20)
                    if not block:
                        break
                    if job.get("purged") or job["cancel"].is_set():
                        raise ConnectionError("The page session ended.")
                    self.wfile.write(block)
                    self.wfile.flush()
                    sent += len(block)
                    if track:
                        now = time.monotonic()
                        with jobs.lock:
                            if not job.get("purged"):
                                job["delivery_by_file"][file_index] = sent
                                job["delivery_sent"] = sum(job["delivery_by_file"].values())
                                total = max(1, job["delivery_total"])
                                pct = min(99.9, 50 + 50 * job["delivery_sent"] / total)
                                speed = sent / max(.001, now - started)
                                left = max(0, total - job["delivery_sent"])
                                job.update(percent=round(pct, 1),
                                           speed=speed, eta=left / speed if speed else None,
                                           message=f"Sending to browser: {job['delivery_sent']/1048576:.1f} of {total/1048576:.1f} MB")
                if sent != size:
                    raise ConnectionError("The browser transfer ended early.")
                transferred = True
            except (ConnectionError, OSError, TimeoutError):
                # The browser may cancel a large transfer; retain the server copy so it can be retried.
                pass
        if track:
            with jobs.lock:
                job["delivery_active"].discard(file_index)
                if transferred and not job.get("purged"):
                    job["delivery_done"].add(file_index)
                    job["delivery_by_file"][file_index] = size
                    job["delivery_sent"] = sum(job["delivery_by_file"].values())
                    if len(job["delivery_done"]) == len(job["files"]):
                        job.update(status="completed", message="Saved to your browser", speed=None, eta=None,
                                    finished=time.time(), percent=100)
                    else:
                        job.update(status="sending", message="Sending remaining files to your browser", finished=0)
                elif not job.get("purged"):
                    job["delivery_by_file"].pop(file_index, None)
                    job["delivery_sent"] = sum(job["delivery_by_file"].values())
                    active = bool(job["delivery_active"])
                    pct = 50 + 50 * job["delivery_sent"] / max(1, job["delivery_total"])
                    job.update(status="sending" if active else "ready", percent=round(min(99.9, pct), 1),
                                speed=None, eta=None,
                                message="Transfer interrupted; choose Save to retry." if not active else "Sending to your browser",
                                finished=0 if active else time.time())

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
            if path.startswith("/api/") and path not in ("/api/session/leave", "/api/session/end"):
                tab = self.tab()
                if not tab or touch(tab) is None:
                    return self.send_json({"error": "This page session has ended. Reload the app to start a fresh session."}, 410)
            if method == "GET":
                if path == "/":
                    if not self.authed():
                        return self.send_html(LOGIN_HTML.replace("{{ERR}}", ""))
                    # Ctrl+Shift+R marks this document to expire its previous tab session.
                    hard = "no-cache" in (self.headers.get("Cache-Control", "") + self.headers.get("Pragma", "")).lower()
                    page = open(os.path.join(HERE, "index.html"), encoding="utf-8").read().replace("{{TOKEN}}", TOKEN).replace("{{FRESH}}", "1" if hard else "0")
                    raw = page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.nocache()
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https: http:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                    if "sv_sid" not in self.cookies():
                        self.send_header("Set-Cookie", f"sv_sid={secrets.token_urlsafe(16)}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if PUBLIC else ""))
                    self.end_headers()
                    return self.wfile.write(raw)
                if path.startswith("/file/"):
                    return self.serve_file(path)
                if path == "/api/info":
                    cur, new = yt_dlp.version.__version__, LATEST["v"]
                    return self.send_json({"ytdlp": cur, "latest": new, "update": bool(new and not PUBLIC and vtuple(new) > vtuple(cur)),
                                           "ffmpeg": bool(FFMPEG), "public": PUBLIC, "can_browser": not PUBLIC and self.is_local(),
                                           "can_cookies": not PUBLIC or bool(APP_PASSWORD), **public_state(self.tab())})
                if path == "/api/ping":
                    return self.send_json({"ok": True})
                if path == "/api/jobs":
                    return self.send_json({"jobs": jobs.listing(self.owner())})
                if path.startswith("/api/search/"):
                    with SEARCH_LOCK:
                        entry = searches.get(path.rsplit("/", 1)[-1])
                        rec = dict(entry["record"]) if entry and entry["owner"] == self.owner() else None
                    return self.send_json(rec if rec is not None else {"error": "Unknown search"}, 200 if rec is not None else 404)
                return self.send_json({"error": "Not found"}, 404)
            data = self.read_json()
            url = str(data.get("url", "")).strip()
            if path == "/api/search":
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Enter a link that starts with http:// or https://")
                check_url(url)
                rec = {"status": "running", "message": "Starting", "percent": 5, "error": "", "fix": "", "result": None}
                sid = uuid.uuid4().hex
                tab = self.tab()
                cancel = threading.Event()
                with SESS_LOCK:
                    if tab in ENDED_TABS:
                        return self.send_json({"error": "This page session has ended. Reload the app to start a fresh session."}, 410)
                    with SEARCH_LOCK:
                        searches[sid] = {"record": rec, "owner": self.owner(), "tab": tab, "cancel": cancel}
                        while len(searches) > 100:  # keep memory bounded
                            old_key = next(iter(searches))
                            old = searches.pop(old_key)
                            old["cancel"].set()
                            old["record"].clear()
                threading.Thread(target=run_search, args=(rec, url, tab, cancel), daemon=True).start()
                return self.send_json({"id": sid})
            if path == "/api/download":
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Search for a valid link first.")
                check_url(url)
                return self.send_json({"id": jobs.create({**data, "url": url}, self.owner())})
            if path.startswith("/api/cancel/"):
                ok = jobs.cancel(path.rsplit("/", 1)[-1], self.owner())
                return self.send_json({"cancelled": ok}, 200 if ok else 409)
            if path == "/api/settings":
                tab = self.tab()
                if not tab:
                    raise ValueError("Reload the page and try again.")
                st = touch(tab)
                if not st:
                    return self.send_json({"error": "This page session has ended. Reload the app to start a fresh session."}, 410)
                if "browser" in data:
                    br = str(data["browser"]).lower()
                    if br and br not in BROWSERS:
                        raise ValueError("Unsupported browser.")
                    if br and (PUBLIC or not self.is_local()):
                        raise PermissionError("Reading a browser login only works on the computer running this app. Use a cookies.txt file instead.")
                    st["browser"] = br
                if data.get("clear_cookies"):
                    remove_path(cookie_path(tab))
                    st.update(cookies=False, domains=[], count=0)
                    if st["method"] == "file":
                        st["method"] = "none"
                txt = data.get("cookies_text")
                if txt:
                    if PUBLIC and not APP_PASSWORD:
                        raise PermissionError("Cookie upload stays off until the server owner sets an APP_PASSWORD.")
                    normalized, n, top = parse_cookies(str(txt))
                    os.makedirs(WORK, exist_ok=True)
                    fd, tmp_path = tempfile.mkstemp(prefix="cookie-", suffix=".txt", dir=WORK)
                    try:
                        # newline=None writes CRLF on Windows, which yt-dlp's Netscape
                        # cookie loader expects, while retaining LF on Unix-like systems.
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write(normalized)
                        parsed_count = validate_cookie_file(tmp_path)
                        if not parsed_count:
                            raise ValueError("No usable cookies were found. Export a fresh cookies.txt and try again.")
                        with SESS_LOCK:
                            if tab in ENDED_TABS or SESS.get(tab) is not st:
                                raise PermissionError("This page session has ended. Reload the app to start a fresh session.")
                            os.replace(tmp_path, cookie_path(tab))
                            st.update(cookies=True, domains=top, count=parsed_count, method="file")
                    finally:
                        if os.path.exists(tmp_path):
                            remove_path(tmp_path)
                m = data.get("method")
                if m in ("none", "browser", "file"):
                    if m == "browser" and not st["browser"]:
                        raise ValueError("Choose a browser first.")
                    if m == "file" and not st["cookies"]:
                        raise ValueError("Upload a cookies.txt file first.")
                    st["method"] = m
                elif "browser" in data:
                    st["method"] = "browser" if st["browser"] else ("none" if st["method"] == "browser" else st["method"])
                return self.send_json(public_state(tab))
            if path == "/api/login/test":
                br = (SESS.get(self.tab()) or {}).get("browser")
                if not br:
                    raise ValueError("Choose a browser first.")
                try:
                    n, top = test_browser_login(br)
                except BaseException as e:
                    return self.send_json({"ok": False, "error": friendly(e)[0]})
                return self.send_json({"ok": True, "count": n, "domains": top})
            if path == "/api/session/leave":
                with SESS_LOCK:
                    st = SESS.get(self.tab())
                    if st and self.tab() not in ENDED_TABS:
                        st["close_at"] = time.time() + 12
                return self.send_json({"ok": True})
            if path == "/api/session/end":
                wipe_tab(self.tab())
                return self.send_json({"ok": True})
            if path.startswith("/api/stop/"):
                ok = jobs.stop(path.rsplit("/", 1)[-1], self.owner())
                return self.send_json({"stopped": ok}, 200 if ok else 409)
            if path == "/api/update":
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "yt-dlp[default]"], capture_output=True, text=True, timeout=300)
                if r.returncode:
                    raise RuntimeError(clean_error(r.stderr or r.stdout))
                return self.send_json({"ok": True, "output": clean_error(r.stdout)})
            if path == "/api/clear":
                jobs.clear(self.owner())
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except (ConnectionError, TimeoutError):
            return   # the browser closed the connection (e.g. cancelled a download); nothing to report
        except PermissionError as e:
            self.safe_error(e, 403)
        except ValueError as e:
            self.safe_error(e, 400)
        except Exception as e:
            log_crash("Unhandled request error")
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
    remove_path(LOG)
    threading.Thread(target=check_latest, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
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
