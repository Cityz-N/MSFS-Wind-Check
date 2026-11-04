"""Winds Aloft viewer. Serves index.html and reads the aircraft position from MSFS over SimConnect (read-only).

Python standard library only. Run WindsAloft.exe or Start.bat, or: python server.py [--no-browser] [--demo]
"""
import ctypes, glob, json, math, os, queue, re, struct, subprocess, sys, threading, time, webbrowser
from ctypes import byref, c_char_p, c_float, c_long, c_ulong, c_void_p
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

if getattr(sys, "frozen", False):  # running as the packaged .exe
    HERE = sys._MEIPASS
    BASE = os.path.dirname(sys.executable)
else:
    HERE = BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8780


def dll_paths():
    """Next to the program first, then the MSFS SDK, then copies other flight sim apps installed."""
    paths = [os.path.join(BASE, "SimConnect.dll"),
             r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll",
             r"C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll"]
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if root:
            paths += glob.glob(os.path.join(root, "*", "SimConnect.dll"))
            paths += glob.glob(os.path.join(root, "*", "*", "SimConnect.dll"))
    return paths



# (name, simvar, units) in the order SimConnect returns them
SIMVARS = [
    ("lat", "PLANE LATITUDE", "degrees"),
    ("lon", "PLANE LONGITUDE", "degrees"),
    ("pressure_alt_ft", "PRESSURE ALTITUDE", "feet"),
    ("true_alt_ft", "PLANE ALTITUDE", "feet"),
    ("heading_true", "PLANE HEADING DEGREES TRUE", "degrees"),
    ("on_ground", "SIM ON GROUND", "bool"),
    ("wind_dir", "AMBIENT WIND DIRECTION", "degrees"),
    ("wind_spd", "AMBIENT WIND VELOCITY", "knots"),
    ("oat_c", "AMBIENT TEMPERATURE", "celsius"),
]
RECV_EXCEPTION, RECV_QUIT, RECV_SIMOBJECT_DATA = 1, 3, 8
RECV_FACILITY_DATA, RECV_FACILITY_END, RECV_FACILITY_LIST = 28, 29, 30
DATATYPE_FLOAT64, PERIOD_SECOND, UNUSED, USER_OBJECT = 4, 4, 0xFFFFFFFF, 0
DATA_OFFSET = 40  # SIMCONNECT_RECV_SIMOBJECT_DATA and _FACILITY_DATA headers (pack 1): 3 + 7 DWORDs
LIST_OFFSET, LIST_ENTRY, ENTRY_LLA = 28, 39, 15  # FACILITY_MINIMAL: 15-byte SIMCONNECT_ICAO, then 3 doubles
STALE_AFTER, RECONNECT_AFTER = 15, 300
RETRY_MIN, RETRY_MAX = 5, 30
LOOKUP_TIMEOUT = 8
HANDOFF_SECONDS, PAGE_GONE_SECONDS, PAGE_CLOSING_SECONDS = 10, 120, 5
# definition ID: (type letter, facility, latitude field, longitude field)
FACILITIES = {
    10: ("W", b"WAYPOINT", b"LATITUDE", b"LONGITUDE"),
    11: ("V", b"VOR", b"VOR_LATITUDE", b"VOR_LONGITUDE"),
    12: ("N", b"NDB", b"LATITUDE", b"LONGITUDE"),
    13: ("A", b"AIRPORT", b"LATITUDE", b"LONGITUDE"),
}
TYPE_NAMES = {"W": "Waypoint", "V": "VOR", "N": "NDB", "A": "Airport"}

state = {"connected": False, "error": None, "position": None, "updated": 0.0, "page_seen": 0.0, "page_closing": 0.0}
lock = threading.Lock()
lookups = queue.Queue()
session_open = threading.Event()


def set_state(**kw):
    with lock:
        state.update(kw)


def load_dll():
    for path in dll_paths():
        if os.path.exists(path):
            try:
                dll = ctypes.WinDLL(path)
                dll.SimConnect_Open
            except (OSError, AttributeError):  # e.g. a 32-bit copy
                continue
            dll.SimConnect_Open.restype = c_long
            dll.SimConnect_Open.argtypes = [ctypes.POINTER(c_void_p), c_char_p, c_void_p, c_ulong, c_void_p, c_ulong]
            dll.SimConnect_Close.restype = c_long
            dll.SimConnect_Close.argtypes = [c_void_p]
            dll.SimConnect_AddToDataDefinition.restype = c_long
            dll.SimConnect_AddToDataDefinition.argtypes = [c_void_p, c_ulong, c_char_p, c_char_p, c_ulong, c_float, c_ulong]
            dll.SimConnect_RequestDataOnSimObject.restype = c_long
            dll.SimConnect_RequestDataOnSimObject.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong]
            dll.SimConnect_GetNextDispatch.restype = c_long
            dll.SimConnect_GetNextDispatch.argtypes = [c_void_p, ctypes.POINTER(c_void_p), ctypes.POINTER(c_ulong)]
            dll.SimConnect_AddToFacilityDefinition.restype = c_long
            dll.SimConnect_AddToFacilityDefinition.argtypes = [c_void_p, c_ulong, c_char_p]
            dll.SimConnect_RequestFacilityData.restype = c_long
            dll.SimConnect_RequestFacilityData.argtypes = [c_void_p, c_ulong, c_ulong, c_char_p, c_char_p]
            dll.SimConnect_GetLastSentPacketID.restype = c_long
            dll.SimConnect_GetLastSentPacketID.argtypes = [c_void_p, ctypes.POINTER(c_ulong)]
            return dll
    return None


def start_lookup(dll, handle, job, next_id):
    """Ask for the name as each facility type; a shared name comes back as a list of matches."""
    job["requests"], job["sends"] = {}, set()
    for define_id, (letter, _, _, _) in FACILITIES.items():
        job["requests"][next_id] = letter
        if dll.SimConnect_RequestFacilityData(handle, define_id, next_id, job["ident"].encode(), b"") < 0:
            job["answered"] += 1
        else:
            send_id = c_ulong()
            dll.SimConnect_GetLastSentPacketID(handle, byref(send_id))
            job["sends"].add(send_id.value)
        next_id += 1
    return next_id


def handle_lookup_message(job, recv_id, raw):
    if recv_id == RECV_EXCEPTION:  # the sim answers a name it doesn't have with an exception
        if struct.unpack_from("<I", raw, 16)[0] in job["sends"]:  # not a late answer to an earlier lookup
            job["answered"] += 1
    elif recv_id == RECV_FACILITY_DATA:
        letter = job["requests"].get(struct.unpack_from("<I", raw, 12)[0])
        if letter and len(raw) >= DATA_OFFSET + 16:
            lat, lon = struct.unpack_from("<2d", raw, DATA_OFFSET)
            job["results"].append({"ident": job["ident"], "letter": letter, "region": "", "lat": lat, "lon": lon})
    elif recv_id == RECV_FACILITY_END:
        if struct.unpack_from("<I", raw, 12)[0] in job["requests"]:
            job["answered"] += 1
    elif recv_id == RECV_FACILITY_LIST:
        request, count, entry, out_of = struct.unpack_from("<4I", raw, 12)
        if request in job["requests"]:
            for i in range(count):
                at = LIST_OFFSET + i * LIST_ENTRY
                if len(raw) < at + LIST_ENTRY:
                    break
                lat, lon = struct.unpack_from("<2d", raw, at + ENTRY_LLA)
                job["results"].append({
                    "ident": raw[at + 1:at + 7].split(b"\0")[0].decode("ascii", "replace"),
                    "letter": raw[at:at + 1].decode("ascii", "replace"),
                    "region": raw[at + 7:at + 10].split(b"\0")[0].decode("ascii", "replace"),
                    "lat": lat, "lon": lon})
            if entry + 1 >= out_of:
                job["answered"] += 1


def finish_lookup(job):
    """The same navaid can come back as a waypoint too: keep one per place, preferring the navaid."""
    merged = []
    for r in sorted(job["results"], key=lambda r: "VNAW".find(r["letter"])):
        same = next((m for m in merged if abs(m["lat"] - r["lat"]) < 0.02 and abs(m["lon"] - r["lon"]) < 0.02), None)
        if same:
            same["region"] = same["region"] or r["region"]
        else:
            merged.append(r)
    job["reply"] = {"results": [{"ident": m["ident"], "type": TYPE_NAMES.get(m["letter"], "Fix"), "region": m["region"],
                                 "lat": round(m["lat"], 5), "lon": round(m["lon"], 5)} for m in merged]}
    if not merged and job["answered"] < len(FACILITIES):  # ran out of time, which doesn't mean the name doesn't exist
        job["reply"]["error"] = "The sim didn't answer in time, please try again"
    job["done"].set()


def read_session(dll, handle):
    """Pump one SimConnect connection until the sim quits or stops sending data."""
    for _, var, units in SIMVARS:
        dll.SimConnect_AddToDataDefinition(handle, 1, var.encode(), units.encode(), DATATYPE_FLOAT64, 0.0, UNUSED)
    dll.SimConnect_RequestDataOnSimObject(handle, 1, 1, USER_OBJECT, PERIOD_SECOND, 0, 0, 0, 0)
    for define_id, (_, name, lat, lon) in FACILITIES.items():
        for field in (b"OPEN " + name, lat, lon, b"CLOSE " + name):
            dll.SimConnect_AddToFacilityDefinition(handle, define_id, field)
    session_open.set()
    last = time.time()
    job, next_id, live = None, 1000, False
    while time.time() - last < RECONNECT_AFTER:  # SimConnect leaks a handle per reconnect, so stay connected
        if live and time.time() - last > STALE_AFTER:
            live = False
            set_state(connected=False, position=None, error="Sim running, waiting for a flight")
        if job and (job["answered"] >= len(FACILITIES) or time.time() > job["deadline"]):
            finish_lookup(job)
            job = None
        if not job:
            try:
                job = lookups.get_nowait()
                job["deadline"] = time.time() + LOOKUP_TIMEOUT
                next_id = start_lookup(dll, handle, job, next_id)
            except queue.Empty:
                pass
        data, size = c_void_p(), c_ulong()
        if dll.SimConnect_GetNextDispatch(handle, byref(data), byref(size)) < 0 or not data.value:
            time.sleep(0.05)
            continue
        raw = ctypes.string_at(data.value, size.value)
        recv_id = struct.unpack_from("<I", raw, 8)[0]
        if job:
            handle_lookup_message(job, recv_id, raw)
        if recv_id == RECV_QUIT:
            break
        if recv_id == RECV_SIMOBJECT_DATA and len(raw) >= DATA_OFFSET + 8 * len(SIMVARS):
            values = struct.unpack_from("<%dd" % len(SIMVARS), raw, DATA_OFFSET)
            last = time.time()
            live = True
            set_state(connected=True, error=None, updated=last,
                      position={name: round(v, 5) for (name, _, _), v in zip(SIMVARS, values)})
    session_open.clear()
    if job:
        finish_lookup(job)
    while not lookups.empty():  # don't let requests for a closed connection pile up
        lookups.get_nowait()["done"].set()


def lookup(ident):
    ident = ident.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", ident):
        return {"results": [], "error": "Not a valid name"}
    if not session_open.is_set():
        return {"results": [], "error": "The sim must be running to look up names"}
    job = {"ident": ident, "answered": 0, "results": [], "requests": {}, "done": threading.Event(), "reply": None}
    lookups.put(job)
    if not job["done"].wait(LOOKUP_TIMEOUT + 2) or job["reply"] is None:
        return {"results": [], "error": "The sim didn't answer"}
    return job["reply"]


def simconnect_loop():
    dll = load_dll()
    if not dll:
        set_state(error="SimConnect.dll not found: see the README, or use Manual")
        return
    retry = RETRY_MIN
    while True:
        handle = c_void_p()
        if dll.SimConnect_Open(byref(handle), b"Winds Aloft", None, 0, None, 0) >= 0:
            retry = RETRY_MIN
            set_state(error="Sim running, waiting for a flight")
            try:
                read_session(dll, handle)
            finally:
                dll.SimConnect_Close(handle)
            set_state(connected=False, position=None)
        else:
            set_state(connected=False, error="Sim not running")
            retry = min(retry * 2, RETRY_MAX)
        time.sleep(retry)


def demo_loop():
    """Fake flight over the North Atlantic, for testing without the sim."""
    start = time.time()
    while True:
        t = time.time() - start
        set_state(connected=True, error=None, updated=time.time(), position={
            "lat": round(51.5 + t * 0.001, 5), "lon": round(-30 + t * 0.004, 5),
            "pressure_alt_ft": 35000 + round(1500 * math.sin(t / 15)), "true_alt_ft": 35640,
            "heading_true": 74.0, "on_ground": 0.0, "wind_dir": 262.0, "wind_spd": 58.0, "oat_c": -54.0})
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def send_body(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path == "/api/closing":
            set_state(page_closing=time.time())
        self.send_body(b"", "text/plain", 204)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        set_state(page_seen=time.time())
        if path == "/api/lookup":
            self.send_body(json.dumps(lookup(parse_qs(url.query).get("id", [""])[0])).encode(), "application/json")
        elif path == "/api/position":
            with lock:
                reply = dict(state, age=round(time.time() - state["updated"], 1) if state["updated"] else None)
            self.send_body(json.dumps(reply).encode(), "application/json")
        elif path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send_body(f.read(), "text/html; charset=utf-8")
        else:
            self.send_body(b"Not found", "text/plain", 404)

    def log_message(self, *args):
        pass


def find_window_browser():
    """Edge ships with Windows and can show a page as a plain app window; Chrome works the same way."""
    for root, rel in ((os.environ.get("ProgramFiles(x86)"), r"Microsoft\Edge\Application\msedge.exe"),
                      (os.environ.get("ProgramFiles"), r"Microsoft\Edge\Application\msedge.exe"),
                      (os.environ.get("LOCALAPPDATA"), r"Microsoft\Edge\Application\msedge.exe"),
                      (os.environ.get("ProgramFiles"), r"Google\Chrome\Application\chrome.exe"),
                      (os.environ.get("ProgramFiles(x86)"), r"Google\Chrome\Application\chrome.exe"),
                      (os.environ.get("LOCALAPPDATA"), r"Google\Chrome\Application\chrome.exe")):
        if root and os.path.exists(os.path.join(root, rel)):
            return os.path.join(root, rel)
    return None


def open_window(url):
    browser = find_window_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile = os.path.join(os.environ.get("LOCALAPPDATA", BASE), "WindsAloft", "window")
    # The throttling flags keep the page updating, and Sim seen recording, while the sim covers the window.
    return subprocess.Popen([browser, "--app=" + url, "--user-data-dir=" + profile, "--window-size=1500,950",
                             "--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling",
                             "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"])


def exit_with_window(window):
    """Stop when the window closes: the browser process ending, the page saying goodbye, or its requests stopping."""
    started = last_tick = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        if now - last_tick > 30:  # the PC slept: give the page time to wake up
            set_state(page_seen=now, page_closing=0.0)
        last_tick = now
        if window and window.poll() is not None:
            if now - started > HANDOFF_SECONDS:
                os._exit(0)
            window = None  # it handed off to a browser already running, which proves nothing about the window
        with lock:
            seen, closing = state["page_seen"], state["page_closing"]
        if closing and seen <= closing and now - closing > PAGE_CLOSING_SECONDS:  # no reload came back
            os._exit(0)
        if seen and now - seen > PAGE_GONE_SECONDS:
            os._exit(0)


def main():
    demo = "--demo" in sys.argv
    url = "http://localhost:%d/" % PORT
    show = "--no-browser" not in sys.argv
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows it would let two copies share the port
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running: just show it
        print("Winds Aloft is already running at %s" % url)
        if show:
            open_window(url)
        return
    threading.Thread(target=demo_loop if demo else simconnect_loop, daemon=True).start()
    print("Winds Aloft running at %s%s." % (url, " (demo position)" if demo else ""))
    if show:
        threading.Thread(target=lambda: exit_with_window(open_window(url)), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
