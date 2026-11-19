#!/usr/bin/env python3
"""Fix XNAT sessions showing "No viewable scans" in the OHIF viewer.

Copyright (c) 2026, Baris Kanber, PhD
Released under the BSD 3-Clause License (see the LICENSE file).

Symptom
-------
Clicking the OHIF Viewer button in XNAT shows:

    No viewable scans: There are no scans in this session compatible
    with the OHIF Viewer.

and the cached viewer metadata (/xapi/viewer/projects/P/experiments/E)
contains series whose "instances" array is empty.

Cause
-----
The OHIF plugin builds its metadata by walking the session's DICOM files
under SCANS/ and matching each file's SeriesInstanceUID against the UID
stored on the scan row (xnat:imageScanData/UID). Sessions ingested by REST
scripts often have scan rows whose UID was never populated, so every
instance is dropped and an empty study list gets cached.

What this script does
---------------------
1. Walks every scan of the experiment (scans with no DICOM files - QC
   snapshots, result archives, etc. - are skipped; OHIF only needs UIDs on
   real image scans).
2. Reads the SeriesInstanceUID from the scan's DICOM files (optionally
   several files, to check consistency).  If the XNAT archive is reachable
   on the local filesystem the files are read directly from
   <archive>/<project>/arc001/<experiment label>/SCANS/<scan>/DICOM;
   otherwise only the first MiB of each file is fetched via REST.
3. POSTs the UID onto the scan row via the XNAT REST field-edit syntax
   (only with --write; by default the script stops after showing what it
   would change).
4. Forces the OHIF plugin to rebuild its cached session metadata
   (POST generate endpoint, with DELETE-invalidate as a fallback for
   older plugin builds), then reports the resulting study/series/instance
   counts.

Usage
-----
    python xnat_fix_ohif_uids.py https://xnat.example.org user EXPERIMENT_ID
        (prompts for the password)

    python xnat_fix_ohif_uids.py https://xnat.example.org user pass EXPERIMENT_ID
    python xnat_fix_ohif_uids.py ... --write        # apply changes (default: dry run,
                                                    # show planned changes only)
    python xnat_fix_ohif_uids.py ... --verify 3      # check 3 files per scan agree
    python xnat_fix_ohif_uids.py ... --insecure      # self-signed TLS certificate
    python xnat_fix_ohif_uids.py ... --archive-root /data/xnat/archive
                                                    # read DICOM headers straight
                                                    # from disk if reachable (default)
    python xnat_fix_ohif_uids.py ... --no-local-files  # always download via REST
    python xnat_fix_ohif_uids.py https://xnat.example.org user pass --all --project P --write
                                                    # fix every experiment in project P
                                                    # (add --write to actually apply;
                                                    #  non-imaging experiments skipped;
                                                    #  add --include-non-imaging to include them)
    python xnat_fix_ohif_uids.py ... --no-colour     # disable coloured output (automatic
                                                    # when stdout is not a terminal)
    python xnat_fix_ohif_uids.py ... --basic-auth    # send credentials with every request
                                                    # (default: one session-token login)

Requires
--------
    pip install requests pydicom

Notes
-----
* Logs in exactly once (POST /data/JSESSIONID) and authenticates all further
  requests with the returned session cookie instead of resending the
  password, so the auth backend (LDAP etc.) never sees repeated logins and
  failed-login lockouts / rate limits are avoided. If the session expires
  mid-run it is re-established with a single automatic re-login, and it is
  closed on exit. If XNAT's siteUrl is https but the script connects over
  plain http, the Secure-flagged session cookie is stripped locally so
  token auth still works (a note is printed; prefer the https URL).
  Deployments that do not honour the session cookie on REST calls at all
  (SSO/proxy front-ends) are detected at login and the script falls back
  to per-request credentials automatically. --basic-auth skips the
  token login entirely.
* Experiments whose uididmap already has entries (i.e. some scan rows
  already carry UIDs, so viewer/ROI references resolve) are skipped by
  default; --overwrite processes them anyway. This protects sessions
  with existing ROI collections from any row-UID changes.
* Needs edit permissions on the experiment; the metadata regeneration step
  may additionally require admin rights on some XNAT versions.
* The UID is taken from the files, i.e. the files are treated as the source
  of truth (that is what the OHIF plugin matches against).
* Direct archive reading only works where the archive path is mounted and
  readable (typically on the XNAT server itself); otherwise the script
  silently falls back to REST header downloads for every scan.
"""

import argparse
import atexit
import copy
import glob
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from getpass import getpass
from io import BytesIO
from urllib.parse import quote, urlparse

HEADER_BYTES = 1 << 20       # 1 MiB is far more than any DICOM header needs
LONG_TIMEOUT = 900           # metadata regeneration can be slow for big sessions
MAX_WORKERS = 1              # default parallel scans; keep modest to be gentle on XNAT
LOCAL_TRY_FILES = 32         # max files inspected per scan dir on the local archive
DEFAULT_ARCHIVE_ROOT = "/data/xnat/archive"
UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*\.?$")

# Extensions that clearly mark non-DICOM content (QC snapshots, archives,
# sidecars, other formats). DICOM files as stored by XNAT are usually
# extensionless or carry .dcm/.ima.
NON_DICOM_EXT = (".gif", ".zip", ".tar", ".gz", ".xml", ".jpg", ".jpeg", ".png",
                 ".bmp", ".txt", ".pdf", ".log", ".csv", ".json", ".html", ".htm",
                 ".nii", ".nrrd", ".mhd", ".dat", ".psv", ".xar", ".svq", ".svs")
DICOM_EXT = (".dcm", ".ima", ".dicom", ".tr5")


def looks_like_dicom(name):
    """Heuristic: does this file name look like a DICOM part-10 file?

    XNAT's DICOM directories hold extensionless numeric names or .dcm/.ima;
    QC artefacts (report gifs, result zips, catalogs) never do.
    """
    base = str(name).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    low = base.lower()
    if low.endswith(NON_DICOM_EXT):
        return False
    if low.endswith(DICOM_EXT):
        return True
    return "." not in base


# --- coloured console output ------------------------------------------------
# ANSI colours for errors/warnings/outcomes. Enabled only when stdout is a
# terminal; disabled by the NO_COLOR environment convention, --no-colour, or
# redirection (so `... | tee log.txt` stays clean). FORCE_COLOR overrides.


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31;1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33;1m"
    CYAN = "\033[36m"


_use_colour = None      # resolved lazily; set to False by --no-colour


def colour_enabled():
    global _use_colour
    if _use_colour is None:
        if os.environ.get("NO_COLOR"):
            _use_colour = False
        elif os.environ.get("FORCE_COLOR"):
            _use_colour = True
        elif os.name == "nt":
            try:
                import colorama
                colorama.init()
                _use_colour = True
            except ImportError:
                _use_colour = False
        else:
            _use_colour = sys.stdout.isatty()
    return _use_colour


def paint(text, colour):
    return f"{colour}{text}{Ansi.RESET}" if colour and colour_enabled() else text


OUTCOME_COLOUR = {"OK": Ansi.GREEN, "DRY": Ansi.YELLOW, "WARN": Ansi.YELLOW,
                  "FAIL": Ansi.RED, "SKIP": Ansi.CYAN}
STATUS_COLOUR = {"ok": Ansi.GREEN, "dry": Ansi.YELLOW, "fail": Ansi.RED,
                 "no-scans": Ansi.CYAN, "skip-has-uids": Ansi.CYAN}   # anything else (error-*) -> red

_print_lock = threading.Lock()


class ApiError(Exception):
    pass


def log(msg="", colour=None):
    print(paint(msg, colour), flush=True)


def short_uid(uid):
    if not uid:
        return "-"
    return uid if len(uid) <= 30 else uid[:19] + "..." + uid[-8:]


CSRF_PARAM = "XNAT_CSRF"
USER_AGENT = "xnat_fix_ohif_uids.py"


def _is_loopback(base):
    """True when the base URL points at the local machine."""
    host = (urlparse(base).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


class SharedLogin:
    """One server-side XNAT session (JSESSIONID cookie + XNAT_CSRF token).

    XNAT re-authenticates every request that carries credentials, which for
    LDAP-backed sites means one directory bind per REST call - exactly the
    traffic pattern that gets accounts flagged or locked. Authenticating
    once and reusing the cookie sidesteps that. The cookie and CSRF token
    live here so the main session and the per-worker connections (the
    requests API is not thread-safe) can share a single login, including
    its single-flight renewal when the session expires mid-run.
    """

    def __init__(self, base, auth, verify):
        self.base = base
        self.auth = auth          # (user, pass); only ever used for login
        self.verify = verify
        self.jar = None           # cookie jar holding the JSESSIONID
        self.csrf = ""
        self.logins = 0
        self.lock = threading.Lock()

    def login(self):
        """Create the token, then verify it actually authenticates REST.

        Returns "ok", or a human-readable reason when the deployment does
        not honour the session cookie on REST calls. XNAT marks the
        JSESSIONID Secure when its siteUrl preference starts with https
        (regardless of the actual request scheme - see SecurityConfig);
        over plain http a Secure cookie is never sent back, so REST calls
        run as guest and 401. That case is recoverable: the cookie's
        Secure flag is stripped locally and the probe retried.
        """
        self._do_login()
        import requests
        s = requests.Session()
        s.verify = self.verify
        s.headers["User-Agent"] = USER_AGENT
        self.adopt(s)
        if not any(c.name == "JSESSIONID" for c in self.jar):
            return ("login returned no JSESSIONID cookie (front-end/proxy may "
                    "have consumed it)")
        status, detail = self._probe_rest(s)
        if status == "ok":
            return "ok"
        if status == "secure-cookie":
            # Plain-HTTP access to a site whose siteUrl is https: the
            # Secure cookie exists in the jar but is never transmitted.
            # Sending it anyway is safe here (no network hop to eavesdrop
            # when talking to loopback; the user chose this URL).
            stripped = [copy.copy(c) for c in self.jar]
            for c in stripped:
                c.secure = False
            s2 = requests.Session()
            s2.verify = self.verify
            s2.headers["User-Agent"] = USER_AGENT
            for c in stripped:
                s2.cookies.set_cookie(c)
            status2, detail2 = self._probe_rest(s2)
            if status2 == "ok":
                self.jar = s2.cookies
                log("[note] Session cookie was marked Secure (XNAT siteUrl is "
                    "https) but the host was reached over plain http; sending it "
                    "anyway for this run. Use the https URL to avoid this.",
                    Ansi.CYAN)
                return "ok"
            detail = detail2
        return (f"server did not honour the session cookie on REST calls "
                f"({detail})")

    def _probe_rest(self, session):
        """Return ("ok"|"secure-cookie"|"fail", human detail) for the cookie.

        GETs /data/projects with cookie-only auth. The secure-cookie case
        is detected by the cookie being flagged Secure while the base URL
        is plain http - the exact configuration (https siteUrl, http
        access) under which it happens.
        """
        try:
            r = session.get(f"{self.base}/data/projects", params={"format": "json"},
                            timeout=60, headers={"Accept": "application/json"})
        except requests.RequestException as e:
            return "ok", ""            # cannot tell; assume the cookie is fine
        if r.status_code == 200:
            return "ok", ""
        if r.status_code in (401, 403):
            jar_has_secure = any(c.secure for c in session.cookies)
            base_is_http = urlparse(self.base).scheme == "http"
            if jar_has_secure and base_is_http:
                return "secure-cookie", (
                    f"cookie is marked Secure (siteUrl is https) but the URL is "
                    f"plain http, so the cookie is never sent; use the https URL "
                    f"or see allowInsecureCookies")
            return "fail", f"GET /data/projects -> HTTP {r.status_code}"
        return "ok", ""                # odd but not an auth rejection

    def _do_login(self):
        import requests
        s = requests.Session()
        s.verify = self.verify
        s.headers["User-Agent"] = USER_AGENT
        s.auth = self.auth
        try:
            r = s.post(f"{self.base}/data/JSESSIONID", params={"CSRF": "true"},
                       headers={"Accept": "*/*"}, timeout=60)
        except requests.RequestException as e:
            raise SystemExit(f"Login failed: POST {self.base}/data/JSESSIONID: "
                             f"{e.__class__.__name__}: {e}")
        if r.status_code in (401, 403):
            raise SystemExit(f"Login failed: HTTP {r.status_code} for user "
                             f"{self.auth[0]!r} - check the username/password.")
        if not (200 <= r.status_code < 300):
            raise SystemExit(f"Login failed: POST {self.base}/data/JSESSIONID -> "
                             f"HTTP {r.status_code}: {r.text[:200].strip()}")
        # Body is "<session id>" or "<session id>; XNAT_CSRF=<token>".
        m = re.search(r"XNAT_CSRF\s*=\s*([^\s;]+)", r.text or "")
        self.jar = s.cookies
        self.csrf = m.group(1) if m else ""
        self.logins += 1
        if not self.csrf:
            log("[WARN] XNAT returned no XNAT_CSRF token; if this site enforces "
                "CSRF, writes may fail with HTTP 403 - retry with --basic-auth.",
                Ansi.YELLOW)

    def adopt(self, session):
        """Copy this login's cookie into `session` (domain preserved, so the
        JSESSIONID is never sent to other hosts, e.g. redirect targets)."""
        if self.jar is not None:
            for c in self.jar:
                session.cookies.set_cookie(copy.deepcopy(c))

    def ensure_alive(self, session):
        """Re-login if the shared session really expired (single-flight).

        Several workers can hit the same expiry at once; the lock plus a
        re-probe mean exactly one re-login happens and the others just
        adopt the revived cookie.
        """
        with self.lock:
            self.adopt(session)
            if not self._probe_dead(session):
                return
            log("Session expired - re-authenticating...", Ansi.YELLOW)
            self._do_login()
            self.adopt(session)

    def _probe_dead(self, session):
        """True if an authenticated GET of /data/JSESSIONID returns 401.

        A 401 from a data URL alone is ambiguous - the viewer regeneration
        endpoints, for instance, return it for insufficient permissions -
        so expiry is confirmed here before re-login: a re-login loop on a
        permission denial would repeat the credential use this class
        exists to avoid. (XnatSecureGuard invalidates the session whenever
        it answers 401, so a live probe cannot resurrect a dead one.)
        """
        try:
            r = session.get(f"{self.base}/data/JSESSIONID", timeout=30,
                            headers={"Accept": "*/*"})
        except Exception:
            return False
        return r.status_code == 401

    def logout(self):
        if self.jar is None:
            return
        import requests
        s = requests.Session()
        s.verify = self.verify
        s.headers["User-Agent"] = USER_AGENT
        self.adopt(s)
        try:
            s.delete(f"{self.base}/data/JSESSIONID", timeout=30,
                     params={CSRF_PARAM: self.csrf} if self.csrf else None,
                     headers={"Accept": "*/*"})
        except Exception:
            pass
        self.jar = None


class XnatSession:
    """requests.Session wrapper speaking the same call interface (get/post/
    put/delete) while authenticating with a server-side session token.

    One POST /data/JSESSIONID with the credentials; every further request
    carries only the session cookie (plus the XNAT_CSRF parameter on
    writes, which cookie-authenticated POST/PUT/DELETE must echo on sites
    with CSRF protection enabled). If the session expires mid-run it is
    renewed once automatically and the request retried. Pass --basic-auth
    to skip all of this and send credentials per request as before.
    """

    def __init__(self, base, auth=None, verify=True):
        import requests
        self.base = base.rstrip("/")
        self.auth = auth            # kept for SharedLogin (re-)login
        self.shared = None          # SharedLogin once token auth is active
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["User-Agent"] = USER_AGENT
        if auth is not None:
            self.s.auth = auth      # basic-auth mode / pre-login default

    def login(self):
        """Exchange the credentials for a session token; cookie auth after.

        Falls back to per-request credentials (returning a reason string)
        when the server does not honour the session cookie on REST calls.
        """
        self.shared = SharedLogin(self.base, self.auth, self.s.verify)
        note = self.shared.login()
        if note != "ok":
            self.shared.logout()
            self.shared = None
            return note
        self.s.auth = None
        self.shared.adopt(self.s)
        return "ok"

    def worker_session(self):
        """Separate connection for a worker thread, sharing this login.

        requests.Session is not thread-safe, so each REST worker gets its
        own; sharing the login keeps the whole run on one server session.
        In basic-auth fallback mode workers just carry the credentials.
        """
        w = XnatSession(self.base, auth=self.auth, verify=self.s.verify)
        if self.shared is not None:
            w.shared = self.shared
            w.s.auth = None
            self.shared.adopt(w.s)
        return w

    def close(self):
        """Invalidate the server-side session (token mode only)."""
        if self.shared is not None:
            self.shared.logout()
            self.shared = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _apply_csrf(self, method, kw):
        params = kw.pop("params", None)
        if (method.upper() in ("POST", "PUT", "DELETE")
                and self.shared is not None and self.shared.csrf):
            params = dict(params) if params else {}
            params[CSRF_PARAM] = self.shared.csrf
        if params is not None:
            kw["params"] = params

    def request(self, method, url, **kw):
        if self.shared is None:
            return self.s.request(method, url, **kw)
        self._apply_csrf(method, kw)
        r = self.s.request(method, url, **kw)
        if (r.status_code == 401 and url.startswith(self.base)
                and self.shared._probe_dead(self.s)):
            self.shared.ensure_alive(self.s)
            self._apply_csrf(method, kw)     # token changes on re-login
            r = self.s.request(method, url, **kw)
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


XSI_ATTR = "{http://www.w3.org/2001/XMLSchema-instance}type"
XNAT_TYPES_NS = "http://nrg.wustl.edu/xnat"


def xsi_from_root_tag(root):
    """Derive an xsiType from the session XML root element name.

    The canonical XNAT item XML names the root element after the data
    type's schema element - <xnat:MRSession> for xnat:mrSessionData,
    <proc:GenProcData> for proc:genProcData - and some XNAT builds omit
    the xsi:type attribute on the root, so reverse that convention.
    """
    if root is None:
        return ""
    if root.tag.startswith("{"):
        ns, _, tag = root.tag[1:].partition("}")
    else:
        ns, tag = "", root.tag
    local = tag.split(":")[-1]
    if not ns:
        ns = XNAT_TYPES_NS
    prefix = "xnat" if ns == XNAT_TYPES_NS else ns.rstrip("/").rsplit("/", 1)[-1]
    tokens = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*", local)
    if not tokens:
        return ""
    name = tokens[0].lower() + "".join(tokens[1:])
    if not name.endswith("Data"):
        name += "Data"
    return f"{prefix}:{name}"


def get_session_xml(s, base, exp):
    """Fetch the session XML; return (root element or None, status note).

    Relies on the format=xml query parameter and sends no restrictive Accept
    header: XNAT serializes XML as text/xml, so 'Accept: application/xml'
    makes its content negotiation fail with HTTP 406.
    """
    try:
        r = s.get(f"{base}/data/experiments/{quote(exp)}?format=xml", timeout=120)
        if r.status_code == 406:
            r = s.get(f"{base}/data/experiments/{quote(exp)}?format=xml",
                      headers={"Accept": "text/xml"}, timeout=120)
    except Exception as e:
        return None, f"request failed ({e.__class__.__name__})"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return ET.fromstring(r.content), "OK"
    except ET.ParseError:
        snippet = r.text[:60].strip().replace("\n", " ")
        return None, (f"HTTP 200 but not parseable XML "
                      f"({r.headers.get('Content-Type', '?')}; starts {snippet!r})")


def parse_session_xml(root):
    """Extract {project, session_xsi, {scan_id: scan_xsi}} from session XML."""
    project, session_xsi = "", ""
    scan_xsi = {}
    if root is None:
        return project, session_xsi, scan_xsi
    session_xsi = root.get(XSI_ATTR) or xsi_from_root_tag(root)
    project = (root.get("project") or "").strip()
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "project" and el.text and el.text.strip() and not project:
            project = el.text.strip()
        if tag == "scan":
            sid = el.get("ID")
            xsi = el.get("{http://www.w3.org/2001/XMLSchema-instance}type") or "xnat:imageScanData"
            if sid:
                scan_xsi[sid] = xsi
    return project, session_xsi, scan_xsi


def ensure_dependencies():
    for mod in ("requests", "pydicom"):
        try:
            __import__(mod)
        except ImportError:
            sys.exit(f"Missing dependency '{mod}'. Install it with:  pip install {mod}")


def get_json(s, url, timeout=120, params=None):
    q = {"format": "json"}
    if params:
        q.update(params)
    try:
        r = s.get(url, params=q, timeout=timeout)
    except Exception as e:
        raise ApiError(f"GET {url} failed: {e.__class__.__name__}: {e}")
    if r.status_code != 200:
        detail = r.text[:300].strip()
        if "<html" in detail.lower():
            detail = ("login/session not accepted (HTML error page returned) - "
                      "check the account, or run with --basic-auth")
        raise ApiError(f"GET {url} -> HTTP {r.status_code}: {detail}")
    try:
        return r.json()
    except ValueError as e:
        raise ApiError(f"GET {url} -> HTTP 200 but not valid JSON "
                       f"({r.headers.get('Content-Type', '?')}): {str(e)[:120]}")


def _fetch_head(s, url):
    """Read only the first HEADER_BYTES of a file via a Range request."""
    headers = {"Accept": "*/*", "Range": f"bytes=0-{HEADER_BYTES - 1}"}
    buf = bytearray()
    with s.get(url, stream=True, timeout=(15, 120), headers=headers) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=65536):
            buf.extend(chunk)
            if len(buf) >= HEADER_BYTES:
                break
        return bytes(buf)


def read_series_uid(s, url, allow_full_download=False):
    """Return the SeriesInstanceUID from a DICOM file, or None.

    Reads only the file header (Range request). The full-file fallback is
    off by default: it can pull very large files through XNAT's file
    service and is rarely needed (only for non-standard, headerless
    part-10 files).
    """
    import pydicom

    def parse(data, force):
        try:
            ds = pydicom.dcmread(BytesIO(data), stop_before_pixels=True, force=force)
        except Exception:
            return None
        uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
        return uid if UID_RE.match(uid) else None

    try:
        head = _fetch_head(s, url)
    except Exception:
        return None
    uid = parse(head, False)
    if uid:
        return uid
    if not allow_full_download:
        return None
    with _print_lock:
        log(f"    [note] header read failed for {url.rsplit('/', 1)[-1]}; "
            f"downloading full file as fallback", Ansi.YELLOW)
    try:
        r = s.get(url, timeout=(15, 600), headers={"Accept": "*/*"})
        r.raise_for_status()
    except Exception:
        return None
    return parse(r.content, False) or parse(r.content, True)


def read_series_uid_file(path):
    """Return the SeriesInstanceUID from a DICOM file on the local filesystem."""
    import pydicom
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except Exception:
        return None
    uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
    return uid if UID_RE.match(uid) else None


def local_scan_dirs(archive_root, project, label, scan_id, resource):
    """Return local directories that may hold this scan's files, best first.

    Follows the XNAT archive layout
        <root>/<project>/arc001/<experiment label>/SCANS/<scan id>/<resource>
    but matches leniently: any arc### cycle, scan directories that carry a
    '-description' suffix, and any resource directory as a fallback.
    """
    if not archive_root:
        return []
    scan_dirs = []
    for parent in sorted(glob.glob(os.path.join(archive_root, project, "arc*",
                                                label, "SCANS"))):
        exact = os.path.join(parent, str(scan_id))
        if os.path.isdir(exact):
            scan_dirs.append(exact)
            continue
        scan_dirs += sorted(glob.glob(exact + "-*"))
    dirs = []
    for sd in scan_dirs:
        try:
            subs = sorted(x for x in os.listdir(sd) if os.path.isdir(os.path.join(sd, x)))
        except OSError:
            continue
        if resource:
            want = str(resource).lower()
            subs.sort(key=lambda x: 0 if x.lower() == want else 1)
        dirs += [os.path.join(sd, x) for x in subs]
    return dirs


def read_uids_local(scan_dirs, verify):
    """Try each directory in turn; return ([uids...], dir_used) on first success."""
    want = max(1, verify)
    for d in scan_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        uids = []
        tried = 0
        for name in names:
            if tried >= LOCAL_TRY_FILES:
                break
            if name.startswith(".") or not looks_like_dicom(name):
                continue
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            tried += 1  # cap DICOM candidates tried, not raw entries listed
            uid = read_series_uid_file(path)
            if uid:
                uids.append(uid)
                if len(uids) >= want:
                    break
        if uids:
            return uids, d
    return [], None


def file_url(base, exp, scan_id, resource, entry):
    uri = str(entry.get("URI") or "")
    if uri.startswith("http"):
        return uri
    if uri.startswith("/"):
        return base + uri
    name = quote(str(entry.get("Name") or ""))
    return (f"{base}/data/experiments/{quote(exp)}/scans/{quote(scan_id)}"
            f"/resources/{quote(resource)}/files/{name}")


def update_scan_uid(s, base, exp, scan_id, uid, xsitype):
    url = f"{base}/data/experiments/{quote(exp)}/scans/{quote(scan_id)}"
    r = s.post(url, params={"xsiType": xsitype, "UID": uid}, timeout=120,
               headers={"Accept": "*/*"})
    if 200 <= r.status_code < 300:
        return True, f"POST set UID (HTTP {r.status_code})"
    # Fallback: XML body PUT
    local = xsitype.split(":")[-1]
    body = f'<xnat:{local} xmlns:xnat="http://nrg.wustl.edu/xnat" UID="{uid}"/>'
    r2 = s.put(url, params={"inbody": "true", "xsiType": xsitype}, data=body,
               headers={"Content-Type": "application/xml", "Accept": "*/*"}, timeout=120)
    if 200 <= r2.status_code < 300:
        return True, f"PUT set UID (HTTP {r2.status_code})"
    detail = (f"POST HTTP {r.status_code}: {r.text[:160].strip()} | "
              f"PUT HTTP {r2.status_code}: {r2.text[:160].strip()}")
    return False, detail


def process_scan(s, base, exp, scan, verify, dry_run, scan_xsi_map=None,
                 allow_full_download=False, archive_root=None, project="",
                 exp_label=""):
    t0 = time.monotonic()
    scan_xsi_map = scan_xsi_map or {}
    scan_id = str(scan.get("ID", "")).strip()
    desc = " / ".join(str(scan[k]) for k in ("type", "series_description") if scan.get(k))
    current = str(scan.get("UID") or "").strip()
    row = {"scan": scan_id, "desc": desc, "current": current, "new": "",
           "outcome": "FAIL", "note": "", "secs": 0.0, "src": "-"}

    if not scan_id:
        row["note"] = "scan row has no ID"
        return row

    try:
        res_js = get_json(s, f"{base}/data/experiments/{quote(exp)}/scans/{quote(scan_id)}/resources")
        resources = (res_js.get("ResultSet") or {}).get("Result") or []
    except ApiError as e:
        row["note"] = str(e)
        row["secs"] = round(time.monotonic() - t0, 1)
        return row

    if not resources:
        row.update(outcome="SKIP", note="no file resources on this scan")
        row["secs"] = round(time.monotonic() - t0, 1)
        return row

    # Pick the resource holding the DICOM files: prefer the one labeled
    # DICOM; otherwise the first whose file list contains DICOM-looking
    # names. Scans with none (QC snapshots, result archives) are skipped -
    # they are not image series and OHIF does not need a UID on them.
    dicom_entries = None
    labeled = next((r for r in resources if str(r.get("label", "")).lower() == "dicom"), None)
    if labeled is not None:
        resource = str(labeled.get("label"))
    else:
        resource = ""
        for cand in resources:
            label = str(cand.get("label"))
            try:
                files_js = get_json(s, f"{base}/data/experiments/{quote(exp)}/scans/{quote(scan_id)}"
                                       f"/resources/{quote(label)}/files")
                cand_entries = [e for e in (files_js.get("ResultSet") or {}).get("Result") or []
                                if e.get("URI") or e.get("Name")]
            except ApiError:
                continue
            hits = sorted((e for e in cand_entries
                           if looks_like_dicom(str(e.get("Name") or ""))),
                          key=lambda e: str(e.get("Name") or ""))
            if hits:
                resource = label
                dicom_entries = hits
                break
        if not resource:
            names = ", ".join(str(r.get("label")) for r in resources)
            row.update(outcome="SKIP",
                       note=f"no DICOM resource (has: {names}) - QC/derived data, "
                            "OHIF needs no UID here")
            row["secs"] = round(time.monotonic() - t0, 1)
            return row

    # Fast path: read the DICOM headers straight off the local archive.
    uids = []
    if archive_root:
        cand_dirs = local_scan_dirs(archive_root, project, exp_label or exp,
                                    scan_id, resource)
        uids, used_dir = read_uids_local(cand_dirs, verify)
        if uids:
            row["src"] = "local"
            row["note"] = f"read from {os.path.relpath(used_dir, archive_root)}"

    # Fall back to downloading just the file headers over REST.
    if not uids:
        if dicom_entries is not None:
            entries = dicom_entries  # already fetched and filtered
        else:
            try:
                files_js = get_json(s, f"{base}/data/experiments/{quote(exp)}/scans/{quote(scan_id)}"
                                       f"/resources/{quote(resource)}/files")
                entries = (files_js.get("ResultSet") or {}).get("Result") or []
            except ApiError as e:
                row["note"] = (row["note"] + " | " if row["note"] else "") + str(e)
                row["secs"] = round(time.monotonic() - t0, 1)
                return row
            entries = [e for e in entries if e.get("URI") or e.get("Name")]
            if not entries:
                row.update(outcome="SKIP",
                           note=(row["note"] + " | " if row["note"] else "")
                                + f"resource '{resource}' lists no files")
                row["secs"] = round(time.monotonic() - t0, 1)
                return row
            # try DICOM-looking names first, but fall back to everything
            prefer = sorted((e for e in entries
                             if looks_like_dicom(str(e.get("Name") or ""))),
                            key=lambda e: str(e.get("Name") or ""))
            entries = prefer or sorted(entries, key=lambda e: str(e.get("Name") or ""))
        row["src"] = "rest"
        for entry in entries[:max(1, verify)]:
            uid = read_series_uid(s, file_url(base, exp, scan_id, resource, entry),
                                  allow_full_download)
            if uid:
                uids.append(uid)
    if not uids:
        row.update(note=f"could not read a SeriesInstanceUID from '{entries[0].get('Name')}' "
                        f"(resource '{resource}'); not a DICOM file, or header read failed "
                        f"(retry with --full-download)")
        row["secs"] = round(time.monotonic() - t0, 1)
        return row

    new_uid = Counter(uids).most_common(1)[0][0]
    row["new"] = new_uid
    distinct = sorted(set(uids))
    mixed = len(distinct) > 1

    if current == new_uid and not mixed:
        row.update(outcome="OK", note="scan row UID already matches the files")
        row["secs"] = round(time.monotonic() - t0, 1)
        return row
    if mixed:
        row["note"] = f"WARNING: {len(distinct)} distinct SeriesInstanceUIDs among files; using the most common"

    if dry_run:
        row["outcome"] = "DRY"
        add = "would set UID" + (" (replacing different row UID)" if current else "")
        row["note"] = add if not row["note"] else row["note"] + " | " + add
        row["secs"] = round(time.monotonic() - t0, 1)
        return row

    xsitype = str(scan_xsi_map.get(scan_id) or scan.get("xsiType")
                  or "xnat:imageScanData").strip()
    ok, detail = update_scan_uid(s, base, exp, scan_id, new_uid, xsitype)
    if ok:
        row["outcome"] = "WARN" if mixed else "OK"
        if current and current != new_uid:
            detail += " (replaced differing row UID)"
        row["note"] = (row["note"] + " | " if row["note"] else "") + detail
    else:
        row["note"] = (row["note"] + " | " if row["note"] else "") + detail
    row["secs"] = round(time.monotonic() - t0, 1)
    return row


def regenerate_metadata(s, base, project, exp):
    """Force the viewer plugin to rebuild its cached session JSON, then verify.

    Plugin builds differ in what they expose on
    /xapi/viewer/projects/{p}/experiments/{e}:
      * POST   - "Generates the session JSON for the specified experiment ID"
                 (the documented regeneration endpoint on recent builds)
      * DELETE - invalidates the cached JSON on some builds; the following
                 GET then rebuilds it on demand
    Invalidate first when possible, then generate, then GET to verify.
    """
    url = f"{base}/xapi/viewer/projects/{quote(project)}/experiments/{quote(exp)}"
    notes = []

    rd = s.delete(url, timeout=120, headers={"Accept": "*/*"})
    if rd.status_code in (200, 201):
        notes.append("DELETE invalidated cached metadata")
    elif rd.status_code == 404:
        notes.append("no cached metadata present")
    elif rd.status_code == 405:
        notes.append("DELETE not supported by this plugin build")
    else:
        extra = " (admin rights needed?)" if rd.status_code in (401, 403) else ""
        notes.append(f"DELETE returned HTTP {rd.status_code}{extra}")

    rp = s.post(url, timeout=LONG_TIMEOUT, headers={"Accept": "application/json"})
    if 200 <= rp.status_code < 300:
        notes.append(f"POST generated session JSON (HTTP {rp.status_code})")
    elif rp.status_code == 405:
        notes.append("POST not supported; relying on GET to build on demand")
    elif rp.status_code in (401, 403):
        notes.append(f"POST returned HTTP {rp.status_code} (admin rights needed?)")
    else:
        notes.append(f"POST returned HTTP {rp.status_code}: {rp.text[:120].strip()}")

    r2 = s.get(url, headers={"Accept": "application/json"}, timeout=LONG_TIMEOUT)
    if r2.status_code != 200:
        return {"detail": f"{'; '.join(notes)}; metadata GET failed: "
                          f"HTTP {r2.status_code}: {r2.text[:200].strip()}",
                "instances": -1}
    try:
        js = r2.json()
    except ValueError:
        return {"detail": f"{'; '.join(notes)}; metadata GET returned non-JSON "
                          f"({r2.headers.get('Content-Type')})", "instances": -1}
    studies = js.get("studies") or []
    n_series = sum(len(st.get("series") or []) for st in studies)
    n_inst = sum(len(se.get("instances") or [])
                 for st in studies for se in (st.get("series") or []))
    return {"detail": f"{'; '.join(notes)}; metadata now contains "
                      f"{len(studies)} study(ies), {n_series} series, "
                      f"{n_inst} instance(s)",
            "instances": n_inst}


def uididmap_entries(s, base, project, exp):
    """Return the plugin's uididmap (series UID -> scan ID) or None on error.

    Computed live from the scan rows by the OHIF plugin
    (PluginUtils.getImageScanUidIdMap): every scan whose row UID is set
    appears here. A map with entries therefore means the session already
    has working series-UID references - which is what the --overwrite
    gate checks.
    """
    url = f"{base}/xapi/roi/projects/{quote(project)}/sessions/{quote(exp)}/uididmap"
    try:
        r = s.get(url, timeout=120)
        if r.status_code != 200:
            return None
        return r.json() or {}
    except Exception:
        return None


def uididmap_state(s, base, project, exp):
    entries = uididmap_entries(s, base, project, exp)
    if entries is None:
        return "uididmap endpoint unavailable"
    n = len(entries)
    return f"uididmap now has {n} entr{'y' if n == 1 else 'ies'}"


def list_project_experiments(s, base, project):
    """Return the experiment listing (ID, label, xsiType) for a project."""
    js = get_json(s, f"{base}/data/projects/{quote(project)}/experiments",
                  params={"columns": "ID,label,xsiType"})
    return (js.get("ResultSet") or {}).get("Result") or []


def process_experiment(s, base, exp, args, archive_root):
    """Run the scan-UID backfill and metadata regeneration for one experiment.

    Returns a summary dict:
        status     dry | ok | fail | no-scans | error-fetch | error-fields | error-project
        counts     Counter of per-scan outcomes
        instances  viewer-metadata instance count (None when regeneration was skipped)
    """
    t0 = time.monotonic()
    summary = {"exp": exp, "label": exp, "project": "", "status": "error-fetch",
               "counts": Counter(), "instances": None, "secs": 0.0}

    try:
        info = get_json(s, f"{base}/data/experiments/{quote(exp)}?format=json")
    except ApiError as e:
        log(f"[FAIL] {e}", Ansi.RED)
        return summary
    fields, xsi = {}, ""
    for item in info.get("items") or []:
        meta = item.get("meta") or {}
        # single-item JSON spells it 'xsi:type'; search listings use 'xsiType'
        xsi = xsi or str(meta.get("xsiType") or meta.get("xsi:type") or "")
        fields = item.get("data_fields") or {}
        if fields:
            break
    if not fields:
        log("[FAIL] Could not read experiment metadata; check the experiment ID and permissions.",
            Ansi.RED)
        summary["status"] = "error-fields"
        return summary

    # JSON layout varies across XNAT versions; the session XML is authoritative
    # for xsiTypes (both the session's and each scan's).
    xml_root, xml_note = get_session_xml(s, base, exp)
    xml_project, xml_session_xsi, scan_xsi_map = parse_session_xml(xml_root)
    if not xsi:
        xsi = xml_session_xsi
    if not args.project and not fields.get("project") and xml_project:
        fields["project"] = xml_project

    project = args.project or str(fields.get("project") or "")
    summary["project"] = project
    if not project:
        log("[FAIL] Experiment has no primary project; pass --project.", Ansi.RED)
        summary["status"] = "error-project"
        return summary
    if not xsi:
        meta = ((info.get("items") or [{}])[0].get("meta")) or {}
        log("[WARN] Experiment xsiType could not be determined: the experiment JSON meta "
            f"has neither 'xsiType' nor 'xsi:type' (meta keys: {sorted(meta) or 'none'}) "
            f"and the session XML yielded no type ({xml_note}). Continuing anyway - this "
            "only skips the imaging-session sanity check; the UID fix itself does not "
            "need it.", Ansi.YELLOW)
    elif "Session" not in xsi:
        log(f"[WARN] Experiment xsiType is '{xsi}' - not an imaging session; continuing anyway.",
            Ansi.YELLOW)

    exp_label = str(fields.get("label") or exp)
    summary["label"] = exp_label
    log(f"Experiment     : {exp_label}  [{exp}]")
    log(f"Project        : {project}")
    log(f"Session type   : {xsi or '(unknown - see warning above)'}")
    log("")

    # --overwrite gate: without it, only sessions whose uididmap is empty
    # (or unavailable) are processed. Any entry means at least one scan row
    # already resolves for ROI/viewer purposes, so leave the session alone -
    # partial fixes would risk pointing ROIs keyed to current values at
    # something else.
    if not getattr(args, "overwrite", False):
        entries = uididmap_entries(s, base, project, exp)
        if entries:
            log(f"Skipping - uididmap already has {len(entries)} entr"
                f"{'y' if len(entries) == 1 else 'ies'} (viewer/ROI references "
                f"already resolve). Re-run with --overwrite to force.",
                Ansi.CYAN)
            summary["status"] = "skip-has-uids"
            summary["secs"] = round(time.monotonic() - t0, 1)
            return summary
        if entries is None:
            log("[WARN] uididmap endpoint unavailable - cannot verify the "
                "session has no UID references; processing anyway.",
                Ansi.YELLOW)

    try:
        scans_js = get_json(s, f"{base}/data/experiments/{quote(exp)}/scans")
    except ApiError as e:
        log(f"[FAIL] {e}", Ansi.RED)
        return summary
    scans = (scans_js.get("ResultSet") or {}).get("Result") or []
    if not scans:
        log("No scan rows on this experiment - nothing to fix.", Ansi.YELLOW)
        summary["status"] = "no-scans"
        summary["secs"] = round(time.monotonic() - t0, 1)
        return summary

    # Some XNAT versions include each scan's xsiType in this listing; use it
    # as a fallback when the session XML was unavailable.
    for sc in scans:
        sid = str(sc.get("ID") or "")
        if sid and sc.get("xsiType"):
            scan_xsi_map.setdefault(sid, str(sc["xsiType"]))

    def sort_key(sc):
        sid = str(sc.get("ID", ""))
        return (0, int(sid), "") if sid.isdigit() else (1, 0, sid)

    # requests.Session is not thread-safe; give each worker its own
    # connection, sharing the one server-side login.
    tls = threading.local()

    def worker(scan):
        if getattr(tls, "s", None) is None:
            tls.s = s.worker_session()
        try:
            return process_scan(tls.s, base, exp, scan, args.verify, args.dry_run,
                                scan_xsi_map, args.full_download, archive_root,
                                project, exp_label)
        except SystemExit:
            raise                      # fatal setup errors (e.g. bad login)
        except Exception as e:
            # One broken scan (odd server response, parse hiccup...) must not
            # abort the other scans or, in --all mode, the remaining
            # experiments: report it as a FAIL row and keep going.
            sid = str(scan.get("ID", "")).strip() or "?"
            return {"scan": sid, "desc": "", "current": "", "new": "",
                    "outcome": "FAIL", "note": f"unexpected error: "
                    f"{e.__class__.__name__}: {str(e)[:200]}",
                    "secs": 0.0, "src": "-"}

    n_workers = max(1, min(args.workers, len(scans)))   # guard --workers 0
    log(f"Processing {len(scans)} scan(s) with {n_workers} worker(s)...")
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        results = list(pool.map(worker, sorted(scans, key=sort_key)))

    log("")
    skips = [r for r in results if r["outcome"] == "SKIP"]
    rows = [r for r in results if r["outcome"] != "SKIP"]
    if rows:
        scans_w = max(max(len(str(r["scan"])) for r in rows), len("SCAN"))
        header = (f"{'SCAN':<{scans_w}}  {'OUTCOME':<8} {'SRC':<6} {'ROW UID':<32} "
                  f"{'UID FROM FILES':<32} {'SECS':>6}  NOTE")
        log(header)
        log("-" * len(header))
        for r in rows:
            note = r["note"].lstrip("| ").strip() if r["note"] else ""
            line = (f"{str(r['scan']):<{scans_w}}  {r['outcome']:<8} {r['src']:<6} "
                    f"{short_uid(r['current']):<32} {short_uid(r['new']):<32} "
                    f"{r['secs']:>6.1f}  {note}")
            line = line.rstrip()
            log(line, OUTCOME_COLOUR.get(r["outcome"]))
        log("-" * len(header))
    if skips:
        w = max(len(str(r["scan"])) for r in skips)
        log(f"Skipped {len(skips)} scan(s) - no DICOM image data to fix:", OUTCOME_COLOUR["SKIP"])
        for r in skips:
            m = re.search(r"\(has: (.*?)\)", r["note"])
            detail = f"resources: {m.group(1)}" if m else r["note"]
            log(f"  {str(r['scan']):<{w}}  {detail}", OUTCOME_COLOUR["SKIP"])
    counts = Counter(r["outcome"] for r in results)
    srcs = Counter(r["src"] for r in results if r["src"] != "-")
    extra = ("  (file reads: " + ", ".join(f"{k}={v}" for k, v in sorted(srcs.items())) + ")"
             if srcs else "")
    log("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + extra)
    log(f"Scan processing total: {sum(r['secs'] for r in results):.1f}s "
        f"(parallel wall time may be lower)")
    summary["counts"] = counts

    if args.dry_run:
        log("\nDry run complete - nothing was modified. Re-run with --write to apply.",
            Ansi.GREEN)
        summary["status"] = "dry"
        summary["secs"] = round(time.monotonic() - t0, 1)
        return summary

    log("\nRegenerating OHIF viewer metadata (may take a while for large sessions)...")
    regen = regenerate_metadata(s, base, project, exp)
    log(regen["detail"],
        Ansi.GREEN if regen["instances"] > 0 else Ansi.RED)
    log(uididmap_state(s, base, project, exp))
    summary["instances"] = regen["instances"]

    if regen["instances"] <= 0:
        log("\nMetadata still shows no instances. Likely causes:", Ansi.YELLOW)
        log("  * Regeneration failed - see the POST/DELETE notes above. An admin")
        log("    can force it manually:")
        log(f"      curl -u ADMIN:*** -X POST {base}/xapi/viewer/projects/{quote(project)}"
            f"/experiments/{quote(exp)}")
        log("  * A stale cached session is still being served (watch for")
        log("    'DELETE not supported'); restarting Tomcat forces a rebuild.")
        log("  * Some scans failed to update above - fix those and re-run this script.")
        log("  * Files are not DICOM, or their SOP classes are not in the plugin's")
        log("    displayable whitelist (check xnat.log for 'Series UID ... has a null")
        log("    or empty scan ID').")

    summary["status"] = "ok" if counts.get("FAIL", 0) == 0 else "fail"
    summary["secs"] = round(time.monotonic() - t0, 1)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="xnat_fix_ohif_uids.py",
        description="Backfill missing SeriesInstanceUIDs on XNAT scan rows from the DICOM "
                    "file headers, then regenerate the OHIF viewer session metadata.",
    )
    p.add_argument("host", help="XNAT host, e.g. https://xnat.example.org")
    p.add_argument("username", help="XNAT username")
    p.add_argument("password", nargs="?", help="XNAT password (omit to be prompted)")
    p.add_argument("experiment_id", nargs="?", default=None,
                   help="experiment ID (accession number) or label (omit when using --all)")
    p.add_argument("--project", help="project ID (default: experiment's primary project)")
    p.add_argument("--verify", type=int, default=1, metavar="N",
                   help="read N files per scan to confirm a consistent SeriesInstanceUID (default 1)")
    p.add_argument("--dry-run", action="store_true",
                   help="show planned UID updates, change nothing (this is the default; "
                        "kept for explicitness)")
    p.add_argument("--write", "--apply", dest="write", action="store_true",
                   help="apply the UID updates and regenerate the viewer metadata "
                        "(without this flag the script only reports what it would do)")
    p.add_argument("--overwrite", action="store_true",
                   help="process experiments even when their uididmap already has "
                        "entries (default: skip any session whose viewer/ROI "
                        "references already resolve; only fully-broken sessions "
                        "are processed)")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS certificate verification (self-signed certs)")
    p.add_argument("--workers", type=int, default=MAX_WORKERS, metavar="N",
                   help=f"parallel scans (default {MAX_WORKERS}: sequential; raise to "
                        f"speed up large sessions, at some load cost to XNAT)")
    p.add_argument("--full-download", action="store_true",
                   help="if a file's header cannot be parsed, download the whole file "
                        "as a fallback (slow; only for non-standard DICOM files)")
    p.add_argument("--archive-root", metavar="PATH", default=DEFAULT_ARCHIVE_ROOT,
                   help="XNAT archive root; when <root>/<project>/arc001/<label>/SCANS "
                        "is readable, DICOM headers are parsed straight from disk "
                        f"(default: {DEFAULT_ARCHIVE_ROOT})")
    p.add_argument("--no-local-files", action="store_true",
                   help="never touch the local filesystem; always download file "
                        "headers via REST")
    p.add_argument("--all", action="store_true",
                   help="process every experiment in the project given by --project "
                        "(requires --project)")
    p.add_argument("--include-non-imaging", action="store_true",
                   help="with --all, also process experiments whose type is not an "
                        "imaging session (skipped by default)")
    p.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true",
                   help="disable coloured terminal output (also disabled automatically "
                        "when stdout is not a terminal; NO_COLOR env honoured)")
    p.add_argument("--basic-auth", action="store_true",
                   help="authenticate every request with the username/password "
                        "(the old behaviour) instead of one session-token login")
    args = p.parse_args(argv)

    if args.dry_run and args.write:
        p.error("--dry-run and --write are mutually exclusive")
    if args.all and args.experiment_id:
        p.error("--all and an explicit experiment ID are mutually exclusive")
    if not args.all and not args.experiment_id:
        p.error("provide an experiment ID, or use --all together with --project")
    if args.all and not args.project:
        p.error("--all requires --project")

    args.dry_run = not args.write    # dry run is the default; --write applies

    global _use_colour
    if args.no_colour:
        _use_colour = False

    ensure_dependencies()
    import requests
    if args.insecure:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass

    password = args.password or getpass(f"Password for {args.username}: ")

    base = args.host.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    log(f"XNAT host      : {base}")
    log(f"Auth           : {'session token (single login)' if not args.basic_auth else 'basic auth (credentials per request)'}")

    s = XnatSession(base, auth=(args.username, password), verify=not args.insecure)
    if not args.basic_auth:
        note = s.login()          # single credential use; cookie for the rest
        if note != "ok":
            log(f"Auth           : basic auth fallback ({note}) - each request "
                f"will carry the credentials", Ansi.YELLOW)
        else:
            atexit.register(s.close)

    archive_root = None
    if not args.no_local_files and args.archive_root:
        archive_root = args.archive_root if os.path.isdir(args.archive_root) else None

    if archive_root:
        log(f"Archive        : {archive_root} (readable; DICOM headers read from disk "
            f"where possible)")
    elif args.no_local_files:
        log("Archive        : local file access disabled (--no-local-files)")
    else:
        log(f"Archive        : {args.archive_root} not accessible here - DICOM "
            f"headers downloaded via REST")
    log(f"Mode           : {'DRY RUN (no changes; add --write to apply)' if args.dry_run else 'live'}")
    log("")

    if not args.all:
        summary = process_experiment(s, base, args.experiment_id.strip(), args, archive_root)
        return {"dry": 0, "ok": 0, "fail": 1, "skip-has-uids": 0}.get(summary["status"], 2)

    # Project-wide mode: fix every experiment in the project.
    try:
        exps = list_project_experiments(s, base, args.project)
    except ApiError as e:
        log(f"[FAIL] {e}", Ansi.RED)
        return 2
    if not exps:
        log(f"No experiments found in project '{args.project}'.", Ansi.YELLOW)
        return 2

    todo, non_imaging = [], []
    for e in exps:
        eid = str(e.get("ID") or e.get("label") or "").strip()
        if not eid:
            continue
        etype = str(e.get("xsiType") or "")
        if etype and "Session" not in etype and not args.include_non_imaging:
            non_imaging.append(str(e.get("label") or eid))
            continue
        todo.append(eid)
    if non_imaging:
        shown = ", ".join(non_imaging[:10]) + (" ..." if len(non_imaging) > 10 else "")
        log(f"Skipping {len(non_imaging)} non-imaging experiment(s): {shown}", Ansi.CYAN)
    if not todo:
        log("No imaging experiments to process.", Ansi.YELLOW)
        return 0
    log(f"Processing {len(todo)} experiment(s) in project '{args.project}'...")
    log("")

    summaries = []
    for i, eid in enumerate(todo, 1):
        log("=" * 126)
        log(f"Experiment {i}/{len(todo)}")
        log("=" * 126)
        try:
            summaries.append(process_experiment(s, base, eid, args, archive_root))
        except SystemExit:
            raise                  # fatal (bad credentials etc.): stop the run
        except Exception as e:
            # Keep processing the remaining experiments; report this one as
            # errored so the summary reflects it.
            log(f"[FAIL] Experiment {eid}: unexpected error: "
                f"{e.__class__.__name__}: {str(e)[:300]}", Ansi.RED)
            summaries.append({"exp": eid, "label": eid, "project": args.project,
                              "status": "error-unexpected",
                              "counts": Counter(), "instances": None, "secs": 0.0})
        log("")

    log("=" * 126)
    log(f"Project summary: {args.project}")
    log("-" * 126)
    label_w = max([len(str(r["label"] or r["exp"])) for r in summaries] + [len("EXPERIMENT")])
    scan_counts = [", ".join(f"{k}={v}" for k, v in sorted(r["counts"].items())) or "-"
                   for r in summaries]
    scans_w = max([len(c) for c in scan_counts] + [len("SCANS")])
    log(f"{'EXPERIMENT':<{label_w}}  {'STATUS':<9} {'INSTANCES':>9}  {'SCANS':<{scans_w}}")
    for r, c in zip(summaries, scan_counts):
        inst = "-" if r["instances"] is None else str(r["instances"])
        line = (f"{str(r['label'] or r['exp']):<{label_w}}  {r['status']:<9} "
                f"{inst:>9}  {c:<{scans_w}}")
        log(line, STATUS_COLOUR.get(r["status"], Ansi.RED))
    log("-" * 126)
    n_fail_scans = sum(r["counts"].get("FAIL", 0) for r in summaries)
    n_err = sum(1 for r in summaries if r["status"].startswith("error"))
    n_skip_uids = sum(1 for r in summaries if r["status"] == "skip-has-uids")
    verdict = (f"{len(summaries)} experiment(s): "
               f"{sum(1 for r in summaries if r['status'] in ('ok', 'dry'))} completed cleanly, "
               f"{sum(1 for r in summaries if r['status'] == 'fail')} with failed scans, "
               f"{n_err} errored, "
               f"{sum(1 for r in summaries if r['status'] == 'no-scans')} had no scans, "
               f"{n_skip_uids} skipped with existing UIDs (--overwrite to reprocess)")
    log(verdict, Ansi.GREEN if n_fail_scans == 0 and n_err == 0 else Ansi.YELLOW)
    return 0 if n_fail_scans == 0 and n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
