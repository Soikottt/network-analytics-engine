"""Auto-block WORKER. A small web service (free Render web service works) that runs the same spam logic as the dashboard, without Streamlit:
   read the Google Sheet -> find confirmed spam -> (shadow mode: only log what it WOULD block | live mode: block ONE Caller ID in Ringba, verify it, log it).
 * Needs analytics_app.py in the same folder (it reuses the app's spam code; nothing is copied, so a new app version is picked up on the next deploy).
 * POST /run  (header X-Worker-Token: <WORKER_TOKEN>)  starts a run, returns at once. Many calls in a row are merged into one run + at most one follow-up run.
 * GET  /health  (no secret, no data)  says "ok": use it as a keep-alive ping.
 * Also runs by itself every WORKER_INTERVAL seconds (default 300) while the service is awake.
Settings (Render > Environment, never in code): GCP_SERVICE_ACCOUNT_JSON (the same service-account JSON the app uses), SHEET_NAME / SHEET_URL, SHEET_TAB, EXTRA_TABS, WORKER_TOKEN,
 TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, RINGBA_ACCOUNT_ID, RINGBA_BLOCK_TOKEN (Admin user's token), SPAM_AUTO, SPAM_AUTO_MODE, SPAM_DRY_RUN, SPAM_AUTO_MIN_CONF, SPAM_AUTO_PER_RUN, SPAM_AUTO_PER_DAY."""
import os, sys, ast, json, time, hmac, threading, traceback, http.server, socketserver
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
_LOG = []


def log(msg):
    msg = _scrub(str(msg))
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)
    _LOG.append(msg)
    del _LOG[:-200]


def _scrub(t):
    for k in ("WORKER_TOKEN", "RINGBA_BLOCK_TOKEN", "RINGBA_API_TOKEN", "TURSO_AUTH_TOKEN", "GCP_SERVICE_ACCOUNT_JSON"):
        v = os.environ.get(k, "")
        if v and len(v) > 6:
            t = t.replace(v, "***")
    return t


# ---------------------------------------------------------------------------------------------------------------------
# load the app's own spam code without Streamlit
# ---------------------------------------------------------------------------------------------------------------------
class _Stub:
    """Stands in for `streamlit`: any attribute / call is harmless; used as a decorator it returns the function unchanged."""
    def __init__(self):
        self.secrets, self.session_state = {}, {}

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return _Stub()

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(())
    def __getitem__(self, k): return _Stub()
    def __bool__(self): return False


def load_core(app_path=None):
    """Executes ONLY the definitions of the app that come before its 'Main flow' (functions, classes, constants, the few registry updates); never the page itself."""
    app_path = app_path or os.environ.get("WORKER_APP_FILE") or os.path.join(HERE, "analytics_app.py")
    src = open(app_path, encoding="utf-8").read()
    pre = src[:src.index("# Main flow")]
    tree = ast.parse(pre)
    import types
    mod = types.ModuleType("app_core")
    sys.modules["app_core"] = mod                  # dataclasses and pickling look the module up by name
    ns = mod.__dict__
    ns.update({"st": _Stub(), "__file__": app_path})
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(n.split(".")[0] == "streamlit" for n in names):
                continue
            try:
                exec(compile(ast.Module([node], []), app_path, "exec"), ns)
            except ImportError:
                for a in node.names:
                    ns[(a.asname or a.name).split(".")[0]] = _Stub()
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            exec(compile(ast.Module([node], []), app_path, "exec"), ns)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) \
                and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id.isupper() and node.value.func.attr in ("update", "extend", "setdefault", "append"):
            exec(compile(ast.Module([node], []), app_path, "exec"), ns)        # e.g. S16_PERMS.update({...}): registries the spam code needs
    return ns


# ---------------------------------------------------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------------------------------------------------
def open_client(core):
    import gspread
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GCP_SERVICE_ACCOUNT_JSON is not set.")
    gc = gspread.service_account_from_dict(json.loads(raw))
    try:
        gc.set_timeout(int(float(os.environ.get("SHEETS_TIMEOUT", "60"))))     # a Google call that hangs must not block every later run
    except Exception:
        pass
    return gc


def load_frame(core, tabs_only=None):
    """The same tabs the dashboard loads (main tab + extra tabs), combined the same way. tabs_only=["Sheet1"] reads just those tabs (the quick run)."""
    pd = core["pd"]
    gc = open_client(core)
    ss, _m = core["open_spreadsheet"](gc, os.environ.get("SHEET_NAME", "Ringba to Sheet QC"), os.environ.get("SHEET_URL", ""))
    if tabs_only:
        tabs = [t.strip() for t in tabs_only if t.strip()]
    else:
        tabs = [os.environ.get("SHEET_TAB", "ALL QC from 30 Sept 2026").strip()]
        for t in os.environ.get("EXTRA_TABS", "Sheet1").split(","):
            t = t.strip()
            if t and t.lower() not in [x.lower() for x in tabs]:
                tabs.append(t)
    frames, titles = [], []
    for i, tab in enumerate(tabs):
        try:
            ws = core["get_worksheet"](ss, tab)
            part = core["worksheet_to_frame"](ws)
        except Exception:
            if i == 0:
                raise
            continue
        if part is not None:
            frames.append(part)
            titles.append(ws.title)
    if not frames:
        raise RuntimeError("The sheet has no data rows.")
    return core["unify_header_aliases"](pd.concat(frames, ignore_index=True).fillna("")), " + ".join(titles), ss


def turso_client(core):
    url, tok = os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", "")
    if not (url and tok):
        raise RuntimeError("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are not set: the worker has nowhere to keep its log.")
    return core["TursoClient"](url, tok, timeout=int(float(os.environ.get("TURSO_TIMEOUT", "15"))))


def _full_due(ledger, now, pd):
    """A FULL read (all tabs) is due when no quick-tab setting exists, it never ran, or the last one is older than WORKER_FULL_HOURS (default 6)."""
    if not os.environ.get("SHEET_FAST_TAB", "").strip():
        return True
    row = ledger.state().get("last_full")
    if not row:
        return True
    try:
        age_h = (pd.Timestamp(now) - pd.Timestamp(row["updated_at"])).total_seconds() / 3600
    except Exception:
        return True
    return age_h >= float(os.environ.get("WORKER_FULL_HOURS", "6")) or age_h < 0


def _rules_since(core, ledger, now, now_s, pd):
    """The rules act only on calls made from this moment on ('from now on'): fixed on the first run and saved in sw_state. SPAM_RULES_SINCE=YYYY-MM-DD (US Eastern midnight), when set,
    OVERRIDES the saved moment (so an earlier start can be chosen at any time). Remove the variable to keep the last value."""
    st = ledger.state().get("rules_since")
    saved = pd.Timestamp(str(st["v"]).strip('"')) if st and str(st.get("v") or "").strip() else None
    env = os.environ.get("SPAM_RULES_SINCE", "").strip()
    if env:
        try:
            since = pd.Timestamp(env).tz_localize(os.environ.get("RINGBA_TIMEZONE", "America/New_York")).tz_convert("UTC").tz_localize(None)
            if saved is None or since != saved:
                ledger.set_state("rules_since", core["s16_ts"](since), now_s)
            return since
        except Exception:
            pass
    if saved is not None:
        return saved
    ledger.set_state("rules_since", core["s16_ts"](now), now_s)
    return now


def run_once(core, why="timer", frame=None, client=None, adapter=None, now=None):
    """-> summary dict. Never raises: an error is logged and stored in sw_state (scrubbed)."""
    pd = core["pd"]
    now = now if now is not None else pd.Timestamp.now("UTC").tz_localize(None)       # always UTC: the app compares against the real UTC clock
    out = {"why": why, "ok": False}
    try:
        client = client or turso_client(core)
        ledger = core["SwLedger"](client=client)
        ledger.init()
        now_s = core["s16_ts"](now)
        ledger.set_state("worker_seen", {"why": why}, now_s)
        if not core["sp_auto_on"]():
            out.update(ok=True, note="SPAM_AUTO is off (kill switch): nothing done.")
            return out
        store = core["S9Store"](":memory:")
        core["sp_init"](store)
        skip = core["sp_auto_import_app_state"](store, client)               # the app's allow-list / rejected / blocked numbers
        full = _full_due(ledger, now, pd)
        if frame is not None:
            df, tabs, ss = (tuple(frame) + (None,))[:3]
        elif full:
            df, tabs, ss = load_frame(core)
        else:
            df, tabs, ss = load_frame(core, [os.environ.get("SHEET_FAST_TAB", "")])     # quick run: only the tab where NEW calls arrive
        scan = core["sp_scan"](store, df, now, tab_default=tabs)
        since = None
        rcfg = None
        try:                                                  # rules (VoIP + silent, wrong number): find the matching calls now, so their callers' history is saved with the rest
            since = _rules_since(core, ledger, now, now_s, pd)
            try:
                ledger.init()
                rcfg = ledger.cfg_get()                                  # rules switched off / added in the app (Auto-block tab)
            except Exception:
                rcfg = None
            core["sp_rules_scan"](store, df, now, since_utc=since, tab_default=tabs, cfg=rcfg)
        except Exception as exc:
            log("rules scan failed: " + type(exc).__name__)
        try:                                                  # a caller's OLD history (QUALIFIED calls in the big tab) must survive a quick read of one tab
            extra = list(ledger.hist_load())
            try:
                extra += list(client.query("SELECT caller_norm, calls, qualified FROM sp_caller_hist"))      # the app's own copy
            except Exception:
                pass
            core["sp_hist_merge"](store, extra, now)
            if full:
                ledger.hist_save(core["sp_hist_export"](store), now_s)
                ledger.set_state("last_full", {"why": why, "rows": int(len(df))}, now_s)
        except Exception as exc:
            log("could not merge caller history: " + type(exc).__name__)
        try:
            core["sp_apply_ledger_blocks"](store, ledger.blocks(active_only=True), now)     # blocks made in earlier runs: this store is rebuilt every run, so it must agree before the sheet is written
        except Exception as exc:
            log("could not apply earlier blocks to this run: " + type(exc).__name__)
        try:
            notes = core["sp_call_notes"](df, full=True)                      # call summaries, shown next to each number in the Auto-block tables
        except Exception:
            notes = {}
        res = core["sp_auto_run"](store, ledger, now, adapter=adapter, skip=skip, notes=notes)
        rres = {}
        try:                                                                  # rules: VoIP + silent, wrong number (this campaign only). Shadow until SPAM_AUTO_RULES_MODE=live.
            rres = core["sp_rules_run"](store, ledger, now, adapter=adapter, skip=skip | set(ledger.blocked_set()), notes=notes, since_utc=since, cfg=rcfg) 
            core["sp_apply_rule_ledger"](store, ledger.rule_blocks(), now)
            res["rules"] = rres
        except Exception as exc:
            log("rules failed: " + type(exc).__name__)
        out.update(ok=True, scan={k: scan.get(k) for k in ("calls", "incidents", "proposals_created") if k in scan}, result=res)
        if ss is not None and core["sp_sheet_sync_on"]():                     # the sheet columns are filled by the worker itself, so they do not wait for someone to open the app
            try:
                sy = core["sp_sheet_sync"](store, ss, now, default_tab=tabs.split(" + ")[0], max_incidents=20000)
                out["sheet"] = {k: sy.get(k) for k in ("ok", "written", "unchanged", "pending", "error")}
                ledger.set_state("last_sheet_sync", out["sheet"], now_s)
                log(f"sheet sync: {sy.get('written', 0)} row(s) written, {sy.get('unchanged', 0)} unchanged, {sy.get('pending', 0)} pending" + ("" if sy.get("ok") else f" - FAILED: {sy.get('error')}"))
            except Exception as exc:
                out["sheet"] = {"ok": False, "error": f"{type(exc).__name__}"}
                log("sheet sync failed: " + type(exc).__name__)
        try:
            rr = res.get("rules") or {}
            log(f"rules (VoIP+silent, wrong number): mode {rr.get('mode', '-')}, matched calls since {core['s16_ts'](since) if since is not None else '-'} UTC: {rr.get('candidates', 0)} number(s) (newly flagged {rr.get('would_block', 0)}, blocked {rr.get('blocked', 0)}, no campaign id {rr.get('no_campaign_id', 0)}, held by limit {rr.get('held_by_limit', 0)}). {rr.get('note', '')}")
        except Exception:
            pass
        ledger.set_state("last_error", "", now_s)
        out["full"] = full
        log(f"run ({why}, {'full read' if full else 'quick read: ' + str(len(df)) + ' row(s)'}): mode {res.get('mode')}, candidates {res.get('candidates')} (newly flagged {res.get('would_block')}, already flagged earlier {res.get('already_flagged')}), blocked {res.get('blocked')}, held by limit {res.get('held_by_limit')}. {res.get('note', '')}")
    except Exception as exc:
        msg = _scrub(f"{type(exc).__name__}: {exc}")[:300]
        out["error"] = msg
        log("run failed: " + msg + " | " + _scrub(traceback.format_exc().splitlines()[-3:][0] if traceback.format_exc() else ""))
        try:
            core["SwLedger"](client=turso_client(core)).set_state("last_error", msg, core["s16_ts"](now))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------------------------------------------------
class Runner:
    """Coalesces triggers: at most one run at a time plus ONE queued follow-up, however many requests arrive.
    Watchdog: a run that has lasted longer than max_run seconds (default 600) is treated as stuck, and the next trigger starts a fresh run instead of queueing behind it forever."""

    def __init__(self, core, run=run_once, max_run=None):
        self.core, self.run, self.lock = core, run, threading.Lock()
        self.max_run = float(os.environ.get("WORKER_MAX_RUN", "600")) if max_run is None else max_run
        self.running, self.again, self.last, self.started, self.gen = False, False, {}, 0.0, 0

    def trigger(self, why="request"):
        with self.lock:
            if self.running:
                if time.time() - self.started <= self.max_run:
                    self.again = True
                    return "queued"
                log(f"the previous run has lasted more than {int(self.max_run)} s: treated as stuck, starting a new run")
            self.running, self.started, self.again = True, time.time(), False
            self.gen += 1
            gen = self.gen
        threading.Thread(target=self._loop, args=(why, gen), daemon=True).start()
        return "started"

    def _loop(self, why, gen):
        while True:
            res = self.run(self.core, why)
            with self.lock:
                if gen != self.gen:                      # a newer run replaced this (stuck) one: stop quietly
                    return
                self.last = res
                self.last["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if not self.again:
                    self.running = False
                    return
                self.again, why, self.started = False, "follow-up", time.time()


def make_handler(runner, token):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "worker"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            given = self.headers.get("X-Worker-Token") or (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            return bool(token) and hmac.compare_digest(str(given).encode(), str(token).encode())

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/health"):
                self._send(200, {"ok": True, "running": runner.running, "last_finished": runner.last.get("finished", "")})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            p = urlparse(self.path).path
            if p != "/run":
                return self._send(404, {"error": "not found"})
            if not token:
                return self._send(503, {"error": "WORKER_TOKEN is not set"})
            if not self._authed():
                log("POST /run refused: the token does not match WORKER_TOKEN")
                return self._send(401, {"error": "unauthorized"})
            res = runner.trigger("request")
            log(f"POST /run accepted ({res})")
            self._send(202, {"result": res})
    return H


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(core, host="0.0.0.0", port=None, interval=None):
    port = int(port or os.environ.get("PORT", "8080"))
    interval = int(interval if interval is not None else float(os.environ.get("WORKER_INTERVAL", "300")))
    runner = Runner(core)
    srv = _Server((host, port), make_handler(runner, os.environ.get("WORKER_TOKEN", "")))
    if interval > 0:
        def tick():
            while True:
                time.sleep(interval)
                runner.trigger("timer")
        threading.Thread(target=tick, daemon=True).start()
    runner.trigger("start")
    log(f"worker listening on {port}")
    return srv, runner


if __name__ == "__main__":
    core = load_core()
    srv, _r = serve(core)
    srv.serve_forever()
