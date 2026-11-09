#!/usr/bin/env python3
"""ANDX one-link gateway.

One URL for every student (bot.andx.ai). A student logs in with their ANDX API
key + secret. The key is validated against ANDX, becomes their identity, and
their own isolated bot container is created on first login (pre-loaded with the
key so they never re-enter it to trade). A signed, http-only, same-site session
cookie keeps them logged in for 30 days, so they don't redo anything. Every
authenticated request is reverse-proxied to that student's own container; no
student can reach another's.
"""
import os, sys, json, time, hmac, hashlib, subprocess, threading, base64, html, shutil
from flask import Flask, request, redirect, make_response, Response, jsonify
import requests as rq
from urllib.parse import urlparse

sys.path.insert(0, "/opt/andx/bot-template")
import andx  # reuse the ANDX client purely to validate a key
import vault  # same encryption the bot uses for secrets.json

ROOT = "/opt/andx"
STUDENTS = os.path.join(ROOT, "students")
PORTS_FILE = os.path.join(ROOT, "ports.json")
SECRET_FILE = os.path.join(ROOT, ".gateway_secret")
KEYS_DIR = "/run/andx-keys"   # tmpfs: per-student keys live in RAM only, gone on reboot
WRAPPED_DIR = os.path.join(ROOT, "wrapped-keys")   # on disk, but only as KMS ciphertext
KMS_KEY = os.environ.get("ANDX_KMS_KEY", "")   # projects/P/locations/L/keyRings/R/cryptoKeys/K
BASE_PORT = 8301
MAX_BOTS = int(os.environ.get("MAX_BOTS", "50"))   # new bots refused past this
_lock = threading.Lock()
_keys = {}   # unwrapped student keys, memory only
if not KMS_KEY:
    sys.exit("ANDX_KMS_KEY is not set: refusing to store student keys unencrypted")

app = Flask(__name__)

# added 1109 -- the ANDX platform embeds this site in an iframe; no other site may frame it
# (stops look-alike pages that frame the key form). The admin console is never framed.
FRAME_ANCESTORS = ("frame-ancestors https://platform.andx.ai https://platform-uat.andx.ai"
                   " https://platform.andx.global https://platform-uat.andx.global")

@app.after_request
def _frame_policy(resp):
    rule = "frame-ancestors 'none'" if request.path.startswith("/admin") else FRAME_ANCESTORS
    csp = resp.headers.get("Content-Security-Policy")
    resp.headers["Content-Security-Policy"] = csp + "; " + rule if csp else rule
    return resp


def _kms(op, data):
    """Cloud KMS encrypt/decrypt; the master key never leaves Google, every call is audit-logged."""
    md = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    token = rq.get(md, headers={"Metadata-Flavor": "Google"}, timeout=5).json()["access_token"]
    field, out = ("plaintext", "ciphertext") if op == "encrypt" else ("ciphertext", "plaintext")
    r = rq.post("https://cloudkms.googleapis.com/v1/%s:%s" % (KMS_KEY, op),
                headers={"Authorization": "Bearer " + token},
                json={field: base64.b64encode(data).decode()}, timeout=10)
    r.raise_for_status()
    result = base64.b64decode(r.json()[out])
    return result

def student_key(uid):
    """Each student's own random Fernet key; stored only wrapped by KMS, unwrapped into memory."""
    if uid in _keys:
        return _keys[uid]
    path = os.path.join(WRAPPED_DIR, uid + ".bin")
    if os.path.exists(path):
        key = _kms("decrypt", open(path, "rb").read())
    else:
        key = base64.urlsafe_b64encode(os.urandom(32))
        os.makedirs(WRAPPED_DIR, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, _kms("encrypt", key)); os.close(fd)
    _keys[uid] = key
    return key

def write_key(uid):
    """Put the student's key in RAM, readable only by the bot user, mounted into their container alone."""
    # /run is root's: the helper makes this student's RAM folder and hands it to us (uid 10001, same as the bot)
    bot("keydir", uid)
    path = os.path.join(KEYS_DIR, uid, "key")
    tmp = path + ".tmp"
    # a leftover read-only tmp (crash mid-write) would block us forever now that we're not root
    if os.path.exists(tmp):
        os.remove(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    os.write(fd, student_key(uid)); os.close(fd)
    os.replace(tmp, path)


def _secret():
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE, "rb").read()
    s = os.urandom(32)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, s); os.close(fd)
    return s
SECRET = _secret()

ACTIVITY_LOG = os.path.join(ROOT, "activity.jsonl")
ADMIN_KEY_FILE = os.path.join(ROOT, ".admin_key")

def _admin_key():
    if not os.path.exists(ADMIN_KEY_FILE):
        fd = os.open(ADMIN_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, os.urandom(12).hex().encode()); os.close(fd)
    return open(ADMIN_KEY_FILE).read().strip()
_admin_key()

def _log_event(uid, event, req):
    """One line per platform event: who, what, when, from where."""
    try:
        rec = {"ts": time.time(),
               "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
               "uid": uid, "event": event,
               "ip": (req.headers.get("X-Forwarded-For") or req.remote_addr or "").split(",")[0].strip()}
        with open(ACTIVITY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

ADMIN_HOURS = 8   # an admin login lasts one working day

def _admin_mac(body):
    # keyed on the admin password too: changing .admin_key logs every admin out
    key = hmac.new(SECRET, b"admin:" + _admin_key().encode(), hashlib.sha256).digest()
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return mac

def _admin_ok():
    """True if the request carries a genuine admin session under 8 hours old, issued after the last admin logout."""
    try:
        name, issued, mac = request.cookies.get("andx_admin", "").split(".")
        issued = int(issued)
    except Exception:
        return False
    body = "%s.%d" % (name, issued)
    ok = (name == "admin" and hmac.compare_digest(mac, _admin_mac(body))
          and time.time() - issued < ADMIN_HOURS * 3600 and issued > _logouts().get("_admin", -1))
    return ok

def _https():
    # Caddy marks https requests; the admin page and its password never travel over plain http
    secure = request.headers.get("X-Forwarded-Proto") == "https"
    return secure

def _admin_headers(resp):
    # the browser runs no script here, never sends this URL onward, never caches the page
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp

ADMIN_LOGIN = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ANDX Bots Admin</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b12;color:#ececf1;
font-family:-apple-system,Segoe UI,sans-serif}form{width:min(92vw,340px);background:#15151f;border:1px solid #262636;
border-radius:14px;padding:26px}input,button{width:100%;box-sizing:border-box;padding:11px;margin:8px 0;border-radius:9px;
font-size:15px}input{background:#0b0b12;border:1px solid #2d2d42;color:#ececf1}button{border:0;background:#7c5cff;
color:#fff;font-weight:600}.err{color:#ff7a7a;font-size:13px}</style></head>
<body><form method=post action=/admin/login><h3>ANDX Bots Admin</h3>{error}
<input name=password type=password placeholder="Admin password" autocomplete=current-password required autofocus>
<button>Sign in</button></form></body></html>"""

@app.get("/admin")
def admin():
    if not _https():
        return page(), 404
    if not _admin_ok():
        return _admin_headers(make_response(ADMIN_LOGIN.replace("{error}", "")))
    return _admin_page()

@app.post("/admin/login")
def admin_login():
    if not _https():
        return page(), 404
    ip = _client_ip()
    # same limiter as students: 5 wrong passwords a minute per address
    if _too_many_logins(ip):
        return _admin_headers(make_response(ADMIN_LOGIN.replace("{error}", "<div class=err>Too many attempts. Wait a minute.</div>"), 429))
    if not hmac.compare_digest(request.form.get("password", ""), _admin_key()):
        _login_failed(ip)
        _log_event("-", "admin login FAILED", request)
        return _admin_headers(make_response(ADMIN_LOGIN.replace("{error}", "<div class=err>Wrong password.</div>"), 401))
    body = "admin.%d" % int(time.time())
    resp = make_response(redirect("/admin"))
    resp.set_cookie("andx_admin", body + "." + _admin_mac(body), max_age=ADMIN_HOURS * 3600, path="/admin",
                    httponly=True, secure=True, samesite="Strict")
    _log_event("-", "admin login", request)
    return resp

@app.post("/admin/logout")
def admin_logout():
    # ends every admin session, not just this browser's
    if _admin_ok():
        with _lock:
            out = _logouts(); out["_admin"] = int(time.time())
            tmp = LOGOUTS_FILE + ".tmp"; json.dump(out, open(tmp, "w")); os.replace(tmp, LOGOUTS_FILE)
    resp = make_response(redirect("/admin"))
    resp.delete_cookie("andx_admin", path="/admin")
    return resp

def _admin_page():
    import concurrent.futures
    ports = _ports()
    def probe(item):
        uid, port = item
        info = {"uid": uid, "status": "no response", "mode": "", "equity": "", "today": "", "created": ""}
        try:
            s = rq.get("http://127.0.0.1:%d/api/state" % port, timeout=3).json()
            info.update(status=s.get("status"), mode=s.get("mode"),
                        equity=s.get("equity"), today=(s.get("stats") or {}).get("today"))
        except Exception:
            pass
        try:
            info["created"] = time.strftime("%Y-%m-%d %H:%M",
                time.localtime(os.path.getmtime(os.path.join(STUDENTS, uid, "config.json"))))
        except Exception:
            pass
        return info
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        rows = list(ex.map(probe, list(ports.items())))
    events = []
    try:
        for line in open(ACTIVITY_LOG).readlines()[-150:]:
            try: events.append(json.loads(line))
            except Exception: pass
    except Exception:
        pass
    events.reverse()
    h = ['<h2>Bots (%d)</h2><table><tr><th>student</th><th>email</th><th>engine</th><th>mode</th>'
         '<th>equity</th><th>trades today</th><th>created</th></tr>' % len(rows)]
    # every cell comes from a student's bot or request headers: escape it so it shows as text, never runs
    esc = lambda v: html.escape(str(v))
    emails = _emails()
    for r0 in rows:
        r0["email"] = emails.get(r0["uid"], "")
        h.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % tuple(esc(r0[k]) for k in ("uid", "email", "status", "mode", "equity", "today", "created")))
    h.append('</table><h2>Recent activity</h2><table><tr><th>time</th><th>student</th><th>event</th><th>ip</th></tr>')
    for ev in events:
        h.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % tuple(esc(ev.get(k)) for k in ("time", "uid", "event", "ip")))
    h.append('</table>')
    style = ('<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#0b0b12;'
             'color:#ececf1;padding:24px}table{border-collapse:collapse;margin:12px 0}'
             'th,td{border:1px solid #333;padding:6px 10px;font-size:13px}th{background:#1a1a26}'
             'h1,h2{font-weight:600}</style>')
    logout = '<form method=post action=/admin/logout style="float:right"><button>Sign out</button></form>'
    body = ('<html><head><meta charset=utf-8><title>ANDX Bots Admin</title>%s</head>'
            '<body>%s<h1>ANDX Bots Admin</h1>%s</body></html>') % (style, logout, ''.join(h))
    resp = _admin_headers(make_response(body))
    return resp

def uid_from_key(api_key):
    return "u" + hashlib.sha256(api_key.encode()).hexdigest()[:16]

SESSION_DAYS = 30        # unused this long -> log in again
REFRESH_AFTER = 86400    # active students get a fresh cookie at most once a day
LOGOUTS_FILE = os.path.join(ROOT, "logouts.json")   # uid -> time of last logout; older cookies are dead

def _logouts():
    try: return json.load(open(LOGOUTS_FILE))
    except Exception: return {}

def sign(uid):
    """Session token uid.issued_at.mac."""
    body = "%s.%d" % (uid, int(time.time()))
    token = body + "." + hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return token

def verify(token):
    """(uid, issued_at) if the token is genuine, under 30 days old and issued after the last logout."""
    try:
        uid, issued, mac = token.split(".")
        issued = int(issued)
    except Exception:
        return None
    good = hmac.compare_digest(mac, hmac.new(SECRET, ("%s.%d" % (uid, issued)).encode(), hashlib.sha256).hexdigest())
    fresh = time.time() - issued < SESSION_DAYS * 86400
    live = issued > _logouts().get(uid, -1)
    session = (uid, issued) if good and fresh and live else None
    return session

def set_session(resp, uid):
    # added 1109 -- always Secure: the session never travels over plain http (the raw-IP http site included)
    resp.set_cookie("andx", sign(uid), max_age=SESSION_DAYS * 86400, httponly=True,
                    secure=True, samesite="Lax")

def _ports():
    try: return json.load(open(PORTS_FILE))
    except Exception: return {}

def _port_for(uid):
    with _lock:
        p = _ports()
        if uid in p: return p[uid]
        port = BASE_PORT
        used = set(p.values())
        while port in used: port += 1
        p[uid] = port
        tmp = PORTS_FILE + ".tmp"; json.dump(p, open(tmp, "w")); os.replace(tmp, PORTS_FILE)
        return port

HELPER = ["sudo", "-n", "/usr/local/sbin/andx-bot"]   # the gateway is not root; this is its only root access

def bot(cmd, uid, *args, check=False):
    r = subprocess.run(HELPER + [cmd, uid] + [str(a) for a in args], capture_output=True, text=True, check=check)
    return r

def container_running(uid):
    r = bot("running", uid)
    return r.returncode == 0 and r.stdout.strip() == "true"

def provision(uid, api_key, api_secret):
    with _lock_for(uid):
        sdir = os.path.join(STUDENTS, uid)
        port = _port_for(uid)
        if not os.path.isdir(sdir):
            subprocess.run(["cp", "-r", os.path.join(ROOT, "bot-template"), sdir], check=True)
            cfg = {"exchange": "andx", "mode": "live", "derivatives": True, "symbols": ["ALL"],
                   "timeframe": "15m", "strategy": "auto", "trade_mode": "auto", "paper_balance": 10000,
                   "strategy_params": {"adx_min": 15}, "bot_style": "balanced",
                   "poll_seconds": 60, "volume_target_usd": 0, "fee_pct_per_side": 0,
                   "max_entries_per_day": 0, "entry_spacing_minutes": 60,
                   "risk": {"risk_per_trade_pct": 1, "atr_stop_mult": 3.0, "max_position_pct": 25,
                            "max_leverage": 2, "max_open_positions": 4, "daily_loss_limit_pct": 5}}
            json.dump(cfg, open(os.path.join(sdir, "config.json"), "w"), indent=2)
        write_key(uid)
        new_key = _saved_key(uid) not in ("", api_key)
        write_secrets(uid, {"andx": {"api_key": api_key, "api_secret": api_secret}})
        if not container_running(uid):
            start_container(uid)
        elif new_key:
            # same student, new API key: restart so the bot trades with it (resumes if it was running)
            bot("restart", uid)

def _saved_key(uid):
    """The API key currently stored for this bot, or blank."""
    try:
        stored = json.load(open(os.path.join(STUDENTS, uid, "secrets.json")))
        key = vault.unseal(stored, key=student_key(uid)).get("andx", {}).get("api_key", "")
    except Exception:
        key = ""
    return key

def write_secrets(uid, secrets):
    """Only ciphertext reaches the disk; the key to read it is in RAM (write_key)."""
    sealed = vault.seal(secrets, key=student_key(uid))
    spath = os.path.join(STUDENTS, uid, "secrets.json")
    tmp = spath + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(sealed, f)
    os.replace(tmp, spath)

def start_container(uid):
    """(Re)create the student's locked-down container with their key mounted (flags live in cloud/andx-bot)."""
    bot("start", uid, _port_for(uid), check=True)

UPGRADE_FILES = ("vault.py", "store.py", "app.py", "templates/dashboard.html")   # bot files changed by these fixes

def _has_key_mount(uid):
    mounted = "/run/andx-key" in bot("keymount", uid).stdout
    return mounted

def reconcile():
    """At start: put every student's key back in RAM, encrypt any plain secrets.json left from
    before, and recreate containers still on the old (unlocked, no key mount) settings."""
    for uid in _ports():
        sdir = os.path.join(STUDENTS, uid)
        if not os.path.isdir(sdir):
            continue
        # same per-student lock as login, so a student logging in meanwhile can't collide with this
        with _lock_for(uid):
            try:
                write_key(uid)
                old = not _has_key_mount(uid)
                if old:
                    # their copy of the bot must understand encrypted secrets first (strategies.py, their edits, untouched)
                    for f in UPGRADE_FILES:
                        shutil.copy(os.path.join(ROOT, "bot-template", f), os.path.join(sdir, f))
                spath = os.path.join(sdir, "secrets.json")
                if os.path.exists(spath):
                    stored = json.load(open(spath))
                    if "fernet" not in stored:
                        write_secrets(uid, stored)
                if old:
                    start_container(uid)
            except Exception as e:
                print("reconcile %s failed: %s" % (uid, e), file=sys.stderr)
    print("reconcile done: %d students" % len(_ports()), file=sys.stderr)

_uid_locks = {}
def _lock_for(uid):
    with _lock:
        if uid not in _uid_locks:
            _uid_locks[uid] = threading.Lock()
        return _uid_locks[uid]

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ANDX Trading Bot</title>
<style>:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#0b0b12;color:#ececf1;font-family:-apple-system,Segoe UI,Roboto,sans-serif}}
.card{{width:min(92vw,380px);background:#15151f;border:1px solid #262636;border-radius:16px;padding:30px}}
h1{{font-size:20px;margin:0 0 4px}}p{{color:#a0a0b4;font-size:14px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:12px 14px;margin:8px 0;background:#0b0b12;
border:1px solid #2d2d42;border-radius:10px;color:#ececf1;font-size:15px}}
button{{width:100%;padding:13px;margin-top:10px;border:0;border-radius:10px;background:#7c5cff;
color:#fff;font-size:15px;font-weight:600;cursor:pointer}}.err{{color:#ff7a7a;font-size:13px;margin:4px 0}}
.hint{{color:#6b6b82;font-size:12px;margin-top:16px;line-height:1.5}}</style></head>
<body><form class=card method=post action=/login>
<h1 style="margin:0 0 6px"><img src=/brand/logo.png alt="ANDX" style="height:30px;display:block"></h1><p>Enter your ANDX API key to start your bot.</p>{error}
<input name=api_key placeholder="ANDX API Key" autocomplete=off required>
<input name=api_secret placeholder="ANDX API Secret" type=password autocomplete=off required>
<button>Start my bot</button>
<div class=hint>Your key stays on your own private bot and never leaves the server. First time sets it up, after that you're remembered.</div>
<div class=hint style="margin-top:10px">New here? <a href=/university style="color:#b7a6ff">Sign up for the monthly trading competition</a></div>
</form></body></html>"""

def page(error=""):
    return PAGE.format(error=("<div class=err>%s</div>" % error) if error else "")

LOGIN_FAILS_PER_IP = 5  # wrong keys per minute from one address (a class on one campus IP still logs in fine)
LOGIN_TOTAL = 60        # attempts per minute in total, so ANDX never rate-limits or blocks our server
_fails = {}             # ip -> recent failure times (gunicorn runs one worker, so memory is shared)
_recent = []            # all recent attempt times

def _client_ip():
    # Caddy sets X-Forwarded-For; the rightmost entry is the address Caddy itself saw
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[-1].strip()
    return ip

def _too_many_logins(ip):
    """Record this attempt; True if this address failed too often or the server as a whole is over its limit."""
    now = time.time()
    with _lock:
        _recent[:] = [t for t in _recent if now - t < 60]
        for k in list(_fails):
            _fails[k] = [t for t in _fails[k] if now - t < 60]
            if not _fails[k]:
                del _fails[k]
        blocked = len(_fails.get(ip, [])) >= LOGIN_FAILS_PER_IP or len(_recent) >= LOGIN_TOTAL
        if not blocked:
            _recent.append(now)
    return blocked

def _login_failed(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())

EMAILS_FILE = os.path.join(ROOT, "emails.json")   # uid -> ANDX account email (gateway-only, bots can't edit it)

def _email_of(client):
    """The key owner's ANDX email via their own key; blank if ANDX won't say (never blocks the login)."""
    try:
        email = (client._gql("{ user { email } }", auth=True)["user"]["email"] or "").strip().lower()
    except Exception:
        email = ""
    return email

def _emails():
    try: return json.load(open(EMAILS_FILE))
    except Exception: return {}

def _uid_for_email(email):
    """The bot already owned by this ANDX account, or None (blank email never matches)."""
    if not email:
        return None
    owned = [u for u, e in _emails().items() if e == email and os.path.isdir(os.path.join(STUDENTS, u))]
    uid = owned[0] if owned else None
    return uid

def _save_email(uid, email):
    if not email:
        return
    with _lock:
        out = _emails(); out[uid] = email
        tmp = EMAILS_FILE + ".tmp"; json.dump(out, open(tmp, "w"), indent=1); os.replace(tmp, EMAILS_FILE)

@app.post("/login")
def login():
    # checked before ANDX is called, so a guessing script never reaches the exchange
    if _too_many_logins(_client_ip()):
        return page("Too many attempts. Please wait a minute and try again."), 429
    key = (request.form.get("api_key") or "").strip()
    sec = (request.form.get("api_secret") or "").strip()
    if not key or not sec:
        return page("Key and secret are both required."), 400
    try:
        client = andx.AndxClient(api_key=key, api_secret=sec)
        client._token()
    except Exception:
        _login_failed(_client_ip())
        return page("That ANDX key didn't work. Double-check the key and secret."), 401
    email = _email_of(client)
    # one bot per student: a new API key from the same ANDX account reuses that account's bot
    uid = _uid_for_email(email) or uid_from_key(key)
    _is_new = not os.path.isdir(os.path.join(STUDENTS, uid))
    # existing students always get in; new bots stop at the cap so the server can't run out of memory
    if _is_new and os.path.isdir(STUDENTS) and len(os.listdir(STUDENTS)) >= MAX_BOTS:
        _log_event(uid, "login refused: server full", request)
        return page("The bot server is full right now. Please contact the competition organisers."), 503
    try:
        provision(uid, key, sec)
    except Exception:
        return page("Couldn't start your bot just now. Please try again."), 500
    if _is_new and email:
        _apply_signup_style(uid, email)
    _save_email(uid, email)
    _log_event(uid, ("login (new bot created)" if _is_new else "login") + (" " + email if email else ""), request)
    resp = make_response(redirect("/"))
    set_session(resp, uid)
    return resp

@app.post("/logout")
def logout():
    # kill every cookie this student holds (all browsers, and any stolen copy), not just this one
    session = verify(request.cookies.get("andx"))
    if session:
        with _lock:
            out = _logouts(); out[session[0]] = int(time.time())
            tmp = LOGOUTS_FILE + ".tmp"; json.dump(out, open(tmp, "w")); os.replace(tmp, LOGOUTS_FILE)
    resp = make_response(redirect("/"))
    resp.delete_cookie("andx")
    return resp

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}

def _same_origin():
    """True if the browser says the request came from this same site (Origin, or Referer when Origin is absent)."""
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    same = bool(src) and urlparse(src).netloc == request.host
    return same

def _proxy(uid, path):
    if not container_running(uid):
        return page("Reconnecting your bot, enter your ANDX key once more."), 200
    # a change (start, stop, keys, settings) must come from our own page, not another site the student has open
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin():
        return page("This request didn't come from your bot page. Please reload and try again."), 403
    port = _port_for(uid)
    url = "http://127.0.0.1:%d/%s" % (port, path)
    hdr = {k: v for k, v in request.headers if k.lower() not in ("host", "cookie")}
    hdr["Origin"] = "http://127.0.0.1:8300"   # checked above; now satisfy the bot's own local same-origin guard
    try:
        r = rq.request(request.method, url, params=request.args, data=request.get_data(),
                       headers=hdr, timeout=60, allow_redirects=False)
    except Exception:
        return page("Your bot is starting up, refresh in a few seconds."), 200
    if request.method == "POST" and path in ("api/start", "api/stop") and r.status_code == 200:
        _log_event(uid, "START bot" if path == "api/start" else "STOP bot", request)
    out = [(k, v) for k, v in r.raw.headers.items() if k.lower() not in _HOP]
    return Response(r.content, r.status_code, out)

@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def root(path):
    session = verify(request.cookies.get("andx"))
    if not session:
        return page()
    uid, issued = session
    resp = make_response(_proxy(uid, path))
    if time.time() - issued > REFRESH_AFTER:
        set_session(resp, uid)
    return resp

# in the background: ~1 s per student would outlast gunicorn's 30 s start-up limit and get the gateway killed
_reconcile_thread = threading.Thread(target=reconcile, daemon=True)
_reconcile_thread.start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)


# ---------------- university signup page (public) ----------------
import re as _re
import time as _time

_SIGNUP_FILE = "/opt/andx/signups.jsonl"
_SIGNUP_RL = {}

_SIGNUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANDX University Trading</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
body{margin:0;background:#0a0a12;color:#e8e8f2;font-family:'IBM Plex Sans',system-ui,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:48px 20px}
h1{font-family:'Space Grotesk',sans-serif;font-size:36px;line-height:1.1;color:#fff;margin:0 0 12px}
p{color:#b9b9cc;line-height:1.6}
label{display:block;font-size:13px;font-weight:600;color:#c6c6da;margin:16px 0 6px}
input,select{width:100%;box-sizing:border-box;background:#0e0e18;color:#e8e8f2;border:1px solid #34344e;border-radius:8px;padding:12px;font-size:15px;font-family:inherit}
input:focus,select:focus{outline:2px solid #8b5cf6}
.bots{display:flex;flex-direction:column;gap:8px;margin-top:6px}
.bots label{display:flex;gap:10px;align-items:flex-start;background:#0e0e18;border:1px solid #34344e;border-radius:10px;padding:12px;margin:0;cursor:pointer;font-weight:400}
.bots input{width:auto;margin-top:3px}
.bots strong{color:#fff;font-size:15px}
.bots span{display:block;font-size:13px;color:#9a9ab0}
button{width:100%;margin-top:22px;background:#8b5cf6;color:#fff;border:none;border-radius:10px;padding:14px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit}
button:hover{background:#7c4ef0}
.err{color:#f87171;font-size:14px;margin-top:12px}
.brand{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:22px;color:#fff;margin-bottom:28px}
.brand b{color:#8b5cf6}
.fine{font-size:12px;color:#73738c;margin-top:18px}
ol{color:#b9b9cc;line-height:1.8;padding-left:20px}
.card{background:#141420;border:1px solid #2a2a40;border-radius:14px;padding:22px;margin-top:20px}
code{background:#0e0e18;border:1px solid #34344e;border-radius:6px;padding:2px 8px;color:#d6ccff}
a{color:#b7a6ff}
</style></head><body><div class="wrap">
<div class="brand"><img src="/brand/logo.png" alt="ANDX" style="height:26px;vertical-align:middle"> &nbsp;<span style="font-size:13px;color:#a3a3b8;letter-spacing:2px;font-weight:500">UNIVERSITY TRADING</span></div>
__BODY__
</div></body></html>"""

_SIGNUP_FORM = """
<h1>Build your bot.<br>Compete every month.</h1>
<p>Sign up once and you are in for every monthly challenge. Pick a starter bot now, change it any time.</p>
<form method="post" action="/university/signup">
  <label for="s_name">Name</label>
  <input id="s_name" name="name" maxlength="80" required>
  <label for="s_school">University</label>
  <select id="s_school" name="school">
    <option>Queens College (CUNY)</option>
    <option>Bowdoin College</option>
    <option>University of South Florida</option>
    <option>Other</option>
  </select>
  <label for="s_email">University email</label>
  <input id="s_email" name="email" type="email" maxlength="120" placeholder="you@school.edu" required>
  <label>Choose your starter bot</label>
  <div class="bots">
    <label><input type="radio" name="bot" value="balanced" checked><span><strong>Balanced Bot</strong><span>Diversified, switches strategy with the market</span></span></label>
    <label><input type="radio" name="bot" value="trend"><span><strong>Trend Bot</strong><span>Turtle-style breakout hunter, rides big moves hard</span></span></label>
    <label><input type="radio" name="bot" value="custom"><span><strong>Custom Bot</strong><span>A classic dip-buyer you edit and make your own</span></span></label>
  </div>
  <button type="submit">Sign me up</button>
  __ERR__
</form>
<p class="fine">Trading involves risk and returns are never guaranteed. We use your email only to run the competition.</p>
<p class="fine">Already signed up? <a href="/">Log in to your bot</a></p>
"""

_SIGNUP_DONE = """
<h1>You're in.</h1>
<p>Welcome to the roster — you are set for every monthly challenge from here. Get your bot live now, it takes about 3 minutes:</p>
<div class="card"><ol>
<li>On the ANDX platform go to Settings &rsaquo; API Keys &rsaquo; Create API key</li>
<li>Name it something plain like <code>my bot</code></li>
<li>Under IP Addresses add <code>136.108.104.209</code> and click Add — required</li>
<li>Turn permissions ON for trading and derivatives, but leave the three <code>api_key</code> ones OFF</li>
<li>Set the expiry date as far out as it allows</li>
<li>Go to <a href="https://bot.andx.ai">bot.andx.ai</a>, log in with the new key, press <b>Start Bot</b> and confirm</li>
</ol></div>
<p class="fine">Your dashboard shows your balance and a log line saying "scanning" when everything is working.</p>
"""

# ------------- ANDX University Trading: competition site v2 -------------
_UNI_DIR = "/opt/andx/university"
_UNI_STUDY = ["Finance", "Computer Science", "Mathematics", "Engineering",
              "Economics", "Business", "Other"]
_UNI_EXP = ["Beginner", "Intermediate", "Experienced"]
_UNI_CODES = {"usf": "University of South Florida",
              "queens": "CUNY Queens College",
              "bowdoin": "Bowdoin College",
              "nyu": "New York University",
              "uconn": "University of Connecticut"}
_UNI_BOTS = [
    {"id": "trend", "name": "Trend Tracker", "hook": "Follow market momentum.",
     "desc": "Identifies market trends and responds to directional price "
             "movements, with exchange-enforced stop-losses on every trade.",
     "type": "Trend Following", "exp": "Beginner Friendly",
     "best": "Students learning market trends", "cta": "Select This Bot"},
    {"id": "momentum", "name": "Momentum Bot", "hook": "React to market movements.",
     "desc": "Uses momentum signals and predefined entry conditions to "
             "evaluate opportunities, standing in cash when momentum fades.",
     "type": "Momentum", "exp": "Intermediate",
     "best": "Students interested in technical analysis", "cta": "Select This Bot"},
    {"id": "grid", "name": "Grid Strategy", "hook": "Explore automated range trading.",
     "desc": "Places trades around a rolling price anchor within a selected "
             "band, a systematic rules-first approach to ranging markets.",
     "type": "Grid", "exp": "Beginner / Intermediate",
     "best": "Students exploring systematic execution", "cta": "Select This Bot"},
    {"id": "custom", "name": "Custom Strategy", "hook": "Build something of your own.",
     "desc": "Start from a working template and edit the actual strategy "
             "code in the built-in editor. Your rules, your indicators.",
     "type": "Custom", "exp": "Advanced",
     "best": "CS, Quant, and Finance students", "cta": "Build Your Own Bot"},
]
_UNI_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z .'-]{0,60}$")
_UNI_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UNI_BTC = {"t": 0.0, "closes": []}


def _uni_bots():
    try:
        with open(_UNI_DIR + "/bots.json") as f:
            return json.load(f)
    except Exception:
        return _UNI_BOTS


def _uni_cfg():
    with open(_UNI_DIR + "/competition.json") as f:
        return json.load(f)


def _uni_records():
    out = []
    try:
        with open(_SIGNUP_FILE) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


@app.get("/university")
def university_page():
    from flask import render_template_string
    from datetime import datetime, timezone
    cfg = _uni_cfg()
    now = datetime.now(timezone.utc)
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1, hour=0,
                          minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1, hour=0, minute=0,
                          second=0, microsecond=0)
    code = (request.args.get("school") or "").lower().strip()
    return render_template_string(
        open(_UNI_DIR + "/university.html").read(),
        study=_UNI_STUDY, experience=_UNI_EXP, bots=_uni_bots(),
        prizes=cfg["prizes"], monthly_total=cfg["monthly_total"],
        approved=cfg.get("prizes_approved", False),
        month=now.strftime("%B"), year=now.year, end_iso=nxt.isoformat(),
        theme=cfg["themes"].get(now.strftime("%B"), {}),
        next_month=nxt.strftime("%B"),
        next_theme=cfg["themes"].get(nxt.strftime("%B"), {}),
        preselect=_UNI_CODES.get(code, ""),
        ref=(request.args.get("ref") or code or "")[:40])


@app.get("/university/btc")
def university_btc():
    import requests as _rq
    now = _time.time()
    if now - _UNI_BTC["t"] > 900 or not _UNI_BTC["closes"]:
        try:
            r = _rq.get("https://api.kraken.com/0/public/OHLC"
                        "?pair=XBTUSD&interval=60", timeout=10).json()
            result = r.get("result") or {}
            pair = next((k for k in result if k != "last"), None)
            closes = [float(x[4]) for x in (result.get(pair) or [])][-200:]
            if len(closes) > 50:
                _UNI_BTC.update(t=now, closes=closes)
        except Exception:
            pass
    return jsonify({"closes": _UNI_BTC["closes"]})


@app.post("/university/signup")
def university_signup():
    ip = request.headers.get("X-Forwarded-For",
                             request.remote_addr or "?").split(",")[0].strip()
    now = _time.time()
    recent = [t for t in _SIGNUP_RL.get(ip, []) if now - t < 3600]
    if len(recent) >= 10:
        return jsonify({"ok": False, "detail": "Too many signups from this "
                        "network right now. Try again in a bit."}), 429
    b = request.get_json(force=True, silent=True) or {}
    if b.get("website"):
        return jsonify({"ok": True})
    first = str(b.get("first", "")).strip()
    last = str(b.get("last", "")).strip()
    school = str(b.get("school", "")).strip()
    email = str(b.get("email", "")).strip().lower()
    study = str(b.get("study", "")).strip()
    exp = str(b.get("experience", "")).strip()
    ref = str(b.get("ref", "")).strip()[:40]
    if not _UNI_NAME_RE.match(first) or not _UNI_NAME_RE.match(last):
        return jsonify({"ok": False, "detail": "Please enter your real first and last name."}), 400
    if not 2 <= len(school) <= 90:
        return jsonify({"ok": False, "detail": "Start typing your university's name and pick it from the list."}), 400
    if not _UNI_EMAIL_RE.match(email):
        return jsonify({"ok": False, "detail": "That email doesn't look right."}), 400
    if study not in _UNI_STUDY:
        study = ""
    if exp not in _UNI_EXP:
        exp = ""
    if not b.get("consent"):
        return jsonify({"ok": False, "detail": "Please agree to the competition "
                        "rules and privacy policy to join."}), 400
    _SIGNUP_RL[ip] = recent + [now]
    existing = [r for r in _uni_records() if r.get("email") == email]
    rec = {"ts": now,
           "time": _time.strftime("%Y-%m-%d %H:%M:%S UTC", _time.gmtime(now)),
           "first": first, "last": last,
           "name": (first + " " + last).strip(), "school": school,
           "email": email, "study": study, "experience": exp,
           "ref": ref, "marketing": bool(b.get("marketing")),
           "bot": existing[-1].get("bot", "") if existing else "", "ip": ip}
    with open(_SIGNUP_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    try:
        os.chmod(_SIGNUP_FILE, 0o600)
    except OSError:
        pass
    return jsonify({"ok": True, "returning": bool(existing)})


@app.post("/university/bot")
def university_bot():
    b = request.get_json(force=True, silent=True) or {}
    email = str(b.get("email", "")).strip().lower()
    bot = str(b.get("bot", "")).strip()
    if bot not in {x["id"] for x in _uni_bots()}:
        return jsonify({"ok": False, "detail": "unknown bot"}), 400
    recs = [r for r in _uni_records() if r.get("email") == email]
    if not recs:
        return jsonify({"ok": False, "detail": "sign up first"}), 400
    rec = dict(recs[-1])
    rec["bot"] = bot
    rec["ts"] = _time.time()
    rec["time"] = _time.strftime("%Y-%m-%d %H:%M:%S UTC", _time.gmtime())
    with open(_SIGNUP_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return jsonify({"ok": True})


@app.get("/university/rules")
def university_rules():
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANDX University Trading | Official Rules</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Instrument+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>body{font:16px/1.65 "Instrument Sans",sans-serif;color:#0d0c12;max-width:760px;
margin:0 auto;padding:40px 24px}h1,h2{font-family:"Space Grotesk",sans-serif}
h1{font-size:30px}h2{font-size:19px;margin-top:28px}.draft{background:#fdf3dd;
border:1px solid #ecc878;border-radius:12px;padding:12px 16px;font-size:14px;
color:#9a6a00;margin:18px 0}a{color:#5a2fe0}</style></head><body>
<h1>ANDX University Trading: Competition Rules</h1>
<div class="draft"><b>DRAFT, pending final approval.</b> These rules are a working draft and
may change before each month's official competition is announced. The published version in
effect at the start of a competition month governs that month.</div>
<h2>1. The program</h2><p>ANDX University Trading is a continuous program of monthly
algorithmic trading competitions. Each calendar month (UTC) is one official competition with
its own leaderboard and prizes. Registration is open year-round; one registration covers all
months.</p>
<h2>2. Eligibility</h2><p>Open to currently enrolled university and college students who
complete registration, hold a verified ANDX account in an eligible jurisdiction, and connect
a bot through the official platform. ANDX employees and immediate family are not eligible for
prizes.</p>
<h2>3. Scoring</h2><p>Ranking is based on risk-controlled trading performance over the
official month: account return over the window, including open positions at market value,
subject to the month's published risk limits. Raw trading volume and trade count do not
improve ranking. The exact formula and any minimum-activity threshold are published before
each month begins.</p>
<h2>4. Fair play</h2><p>One account per participant. Market manipulation, wash trading,
exploiting platform defects, or interfering with other participants means disqualification.
Suspicious activity is reviewed before winners are confirmed.</p>
<h2>5. Technical issues</h2><p>Verified platform-side outages are handled so no participant is
unfairly penalized; the remedy is announced with the affected month's results. Participants
are responsible for their own API keys and configurations.</p>
<h2>6. Prizes</h2><p>Prize amounts and featured rewards are announced before each month and
may change between months. Winner verification happens promptly after month end; prizes are
delivered after verification. Taxes are the recipient's responsibility.</p>
<h2>7. Risk</h2><p>Trading involves risk, including loss of the funds you trade with. Nothing
in this program is investment advice or a promise of profit. Bots never receive withdrawal
permissions.</p>
<h2>8. Privacy</h2><p>Registration data is used to run the program. Leaderboard display names
are opt-in; emails, balances and keys are never shown publicly.</p>
<p style="margin-top:30px"><a href="/university">Back to ANDX University Trading</a></p>
</body></html>"""


@app.get("/brand/<name>")
def brand_asset(name):
    from flask import send_from_directory, abort
    if name not in ("logo.png", "mark.png"):
        abort(404)
    return send_from_directory("/opt/andx/brand", name, max_age=86400)


def _signup_view_key():
    try:
        return open("/opt/andx/.signup_view_key").read().strip()
    except OSError:
        return None


@app.get("/university/signups")
def university_signups_view():
    from flask import abort
    key = _signup_view_key()
    if not key or not hmac.compare_digest(request.args.get("key", ""), key):
        abort(404)
    rows = []
    try:
        with open(_SIGNUP_FILE) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    rows.reverse()
    if request.args.get("format") == "csv":
        out = "time,name,school,email,bot\n"
        for r in rows:
            vals = [str(r.get(k, "")).replace('"', "'") for k in ("time", "name", "school", "email", "bot")]
            out += ",".join('"' + v + '"' for v in vals) + "\n"
        resp = make_response(out)
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = "attachment; filename=signups.csv"
        return resp
    body = ""
    for r in rows:
        cells = "".join("<td>" + str(r.get(k, "")).replace("<", "&lt;") + "</td>"
                        for k in ("time", "name", "school", "email", "bot"))
        body += "<tr>" + cells + "</tr>"
    csv_link = '<a href="/university/signups?key=' + key + '&format=csv">Download CSV</a>'
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Signups</title><style>body{font-family:system-ui;background:#0a0a12;"
            "color:#e8e8f2;padding:24px}table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #2a2a40;padding:8px 10px;font-size:14px;text-align:left}"
            "th{background:#141420}a{color:#b7a6ff}</style></head><body>"
            "<h2>Competition signups (" + str(len(rows)) + ")</h2><p>" + csv_link + "</p>"
            "<table><tr><th>Time</th><th>Name</th><th>School</th><th>Email</th><th>Bot</th></tr>"
            + body + "</table></body></html>")


# ---------------- signup choice -> bot preset at first login ----------------
_STYLE_PRESETS = {
    "balanced": {"strategy": "auto", "bot_style": "balanced",
                 "strategy_params": {"adx_min": 15, "adx_trend": 22, "adx_range": 18}},
    "trend": {"strategy": "donchian", "bot_style": "trend",
              "strategy_params": {"entry": 20, "exit": 10},
              "risk": {"atr_stop_mult": 3.0, "max_open_positions": 3}},
    "custom": {"strategy": "rsi_meanrev", "bot_style": "custom",
               "strategy_params": {"rsi_buy": 30, "rsi_sell": 70, "adx_max": 30}},
    "momentum": {"strategy": "ema_trend", "bot_style": "trend",
                 "strategy_params": {"fast": 21, "slow": 55, "adx_min": 18},
                 "risk": {"atr_stop_mult": 3.0, "max_open_positions": 3}},
    "grid": {"strategy": "lattice", "bot_style": "custom",
             "strategy_params": {"anchor_bars": 96, "grid_step_pct": 1.5},
             "risk": {"atr_stop_mult": 3.0}},
    "scheduled-buy": {"strategy": "clockwork", "bot_style": "custom",
                      "strategy_params": {}, "risk": {"atr_stop_mult": 4.0}},
    "trend-follower": {"strategy": "ema_trend", "bot_style": "trend",
                       "strategy_params": {"fast": 21, "slow": 55, "adx_min": 18},
                       "risk": {"atr_stop_mult": 3.0, "max_open_positions": 3}},
    "grid-trader": {"strategy": "lattice", "bot_style": "custom",
                    "strategy_params": {"anchor_bars": 96, "grid_step_pct": 1.5},
                    "risk": {"atr_stop_mult": 3.0}},
    "portfolio-rebalancer": {"strategy": "keelstone", "bot_style": "custom",
                             "strategy_params": {"anchor_bars": 200, "band_pct": 4.0},
                             "risk": {"atr_stop_mult": 4.0}},
    "mean-reverter": {"strategy": "rsi_meanrev", "bot_style": "custom",
                      "strategy_params": {"rsi_buy": 30, "rsi_sell": 70, "adx_max": 30}},
    "fleet-captain": {"strategy": "auto", "bot_style": "balanced",
                      "strategy_params": {"adx_min": 15, "adx_trend": 22, "adx_range": 18},
                      "risk": {"max_open_positions": 4}},
    "breakout-hunter": {"strategy": "donchian", "bot_style": "trend",
                        "strategy_params": {"entry": 20, "exit": 10},
                        "risk": {"atr_stop_mult": 3.0, "max_open_positions": 3}},
    "accumulator": {"strategy": "metronome", "bot_style": "custom",
                    "strategy_params": {"cadence_bars": 4},
                    "risk": {"atr_stop_mult": 4.0}},
    "dip-ladder": {"strategy": "plumbline", "bot_style": "custom",
                   "strategy_params": {"high_bars": 250, "dip_pct": 5.0},
                   "risk": {"atr_stop_mult": 4.0}},
    "momentum-rotator": {"strategy": "weathervane", "bot_style": "trend",
                         "strategy_params": {"mom_bars": 192},
                         "risk": {"atr_stop_mult": 3.0}},
    "profit-stairs": {"strategy": "staircase", "bot_style": "custom",
                      "strategy_params": {"take_rsi": 72}},
    "income-drip": {"strategy": "hourglass", "bot_style": "custom",
                    "strategy_params": {"drip_bars": 96},
                    "risk": {"atr_stop_mult": 4.0}},
    "volatility-guard": {"strategy": "barometer", "bot_style": "trend",
                         "strategy_params": {"vol_target_pct": 1.2},
                         "risk": {"atr_stop_mult": 3.0}},
    "discount-buyer": {"strategy": "winnow", "bot_style": "custom",
                       "strategy_params": {"sma_bars": 192},
                       "risk": {"atr_stop_mult": 4.0}},
    "pair-trader": {"strategy": "fulcrum", "bot_style": "custom",
                    "strategy_params": {"z_bars": 96, "entry_z": 1.5, "exit_z": 0.3}},
}


def _apply_signup_style(uid, email):
    """New bot + the student picked a style at signup: configure it so their
    first login lands on the bot they chose. Engine reads config at Start."""
    try:
        choice = None
        with open("/opt/andx/signups.jsonl") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if str(r.get("email", "")).lower() == email.lower():
                    choice = r.get("bot")
        preset = _STYLE_PRESETS.get(choice or "")
        if not preset:
            return
        cfgp = os.path.join(STUDENTS, uid, "config.json")
        cfg = json.load(open(cfgp))
        cfg["strategy"] = preset["strategy"]
        cfg["bot_style"] = preset["bot_style"]
        cfg["strategy_params"] = {**(cfg.get("strategy_params") or {}),
                                  **preset.get("strategy_params", {})}
        if preset.get("risk"):
            cfg["risk"] = {**(cfg.get("risk") or {}), **preset["risk"]}
        tmp = cfgp + ".tmp"
        json.dump(cfg, open(tmp, "w"), indent=2)
        os.replace(tmp, cfgp)
        _log_event(uid, "signup style applied: " + preset["bot_style"], request)
    except Exception:
        pass
