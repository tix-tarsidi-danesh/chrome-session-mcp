"""Read and decrypt Google Chrome cookies, and open Chrome on a page.

Chrome stores cookies in an encrypted SQLite database per profile. Values are
AES-128-CBC (IV = 16 spaces) with a key derived by PBKDF2-HMAC-SHA1 (salt
"saltysalt"): 1003 iterations from the Keychain's "Chrome Safe Storage" password
on macOS, 1 iteration from the keyring password (or the fixed "peanuts") on
Linux. Newer Chrome prepends a 32-byte SHA256(host) tag to the plaintext. AES
runs through the system `openssl` binary because Python's stdlib has no AES.

Two things matter for a long-running server:

* The AES key is derived once and kept in memory, so the Keychain / keyring is
  asked once per server process, not once per call.
* The profile a host's cookies came from is remembered, so a re-login opens
  Chrome in that same profile: the one the fresh cookies are read back from.

Settings (env wins over config): CHROME_PROFILE, CHROME_USER_DATA,
CHROME_SAFE_STORAGE_PASSWORD.
"""
import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading

from .util import ToolError, host_of, log

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
PBKDF2_ITERS = 1003 if IS_MAC else 1

# Set from the config file by configure(); environment variables still win.
_settings = {"profile": None, "user_data": None}


def configure(profile=None, user_data=None):
    _settings["profile"] = profile
    _settings["user_data"] = os.path.expanduser(user_data) if user_data else None


def _default_user_data():
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    for cand in ("~/.config/google-chrome", "~/.config/google-chrome-beta",
                 "~/.config/google-chrome-unstable", "~/.config/chromium"):
        p = os.path.expanduser(cand)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~/.config/google-chrome")


def user_data_dir():
    return os.environ.get("CHROME_USER_DATA") or _settings["user_data"] or _default_user_data()


def pinned_profile():
    return os.environ.get("CHROME_PROFILE") or _settings["profile"]


# ------------------------------------------------------------------- key cache
_keys = None
_keys_lock = threading.Lock()


def _derive(password):
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", PBKDF2_ITERS, 16)


def _mac_keychain_password():
    try:
        pw = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise ToolError("could not read 'Chrome Safe Storage' from the macOS Keychain "
                        "(the prompt was cancelled or the item is missing). Retry and click "
                        "'Always Allow'.")
    return pw.rstrip(b"\n")


def _linux_keyring_password():
    env = os.environ.get("CHROME_SAFE_STORAGE_PASSWORD")
    if env is not None:
        return env.encode()
    if shutil.which("secret-tool"):
        for app in ("chrome", "chromium", "google-chrome"):
            try:
                pw = subprocess.check_output(["secret-tool", "lookup", "application", app],
                                             stderr=subprocess.DEVNULL)
                if pw:
                    return pw.rstrip(b"\n")
            except subprocess.CalledProcessError:
                continue
    try:
        import secretstorage  # type: ignore
        conn = secretstorage.dbus_init()
        for coll in secretstorage.get_all_collections(conn):
            if coll.is_locked():
                coll.unlock()
            for item in coll.get_all_items():
                if item.get_label() in ("Chrome Safe Storage", "Chromium Safe Storage"):
                    return item.get_secret()
    except Exception:
        pass
    return None


def _build_keys():
    """Cookie version prefix -> AES key, derived once per process."""
    global _keys
    with _keys_lock:
        if _keys is None:
            if IS_MAC:
                env = os.environ.get("CHROME_SAFE_STORAGE_PASSWORD")
                k = _derive(env.encode() if env is not None else _mac_keychain_password())
                _keys = {"v10": k, "v11": k}
            else:
                keys = {"v10": _derive(b"peanuts")}
                pw = _linux_keyring_password()
                if pw:
                    keys["v11"] = _derive(pw)
                _keys = keys
        return _keys


# ---------------------------------------------------------------------- crypto
def _aes_cbc_decrypt(key, ciphertext):
    out = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
         "-K", key.hex(), "-iv", (b" " * 16).hex()],
        input=ciphertext, capture_output=True,
    )
    return out.stdout if out.returncode == 0 else b""


def _strip_pkcs7(b):
    if b and 1 <= b[-1] <= 16 and b[-b[-1]:] == bytes([b[-1]]) * b[-1]:
        return b[:-b[-1]]
    return b


def _printable(b):
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(32 <= ord(c) < 127 for c in s)


def _decrypt(keys, enc):
    if not enc:
        return ""
    ver = enc[:3]
    if ver in (b"v10", b"v11"):
        key, enc = keys.get(ver.decode()), enc[3:]
    else:
        key = keys.get("v10") or keys.get("v11")
    if key is None:
        return ""
    dec = _strip_pkcs7(_aes_cbc_decrypt(key, enc))
    if _printable(dec):
        return dec.decode("utf-8")
    if len(dec) > 32 and _printable(dec[32:]):
        return dec[32:].decode("utf-8")
    return ""


# ---------------------------------------------------------------- cookie read
def list_profiles():
    return sorted(os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(user_data_dir(), "*", "Cookies")))


def applies(host_key, host):
    """True when a cookie stored for `host_key` is sent to `host`."""
    hk = host_key.lstrip(".")
    return host == hk or host.endswith("." + hk)


def _read_rows(db_path, host):
    parts = host.split(".")
    search = ".".join(parts[-2:]) if len(parts) >= 2 else host
    # Chrome holds the database open and locked; read a copy.
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(tmp)
        con.text_factory = bytes
        rows = con.execute(
            "SELECT host_key, name, encrypted_value, COALESCE(expires_utc,0) "
            "FROM cookies WHERE host_key LIKE ?", (f"%{search}%",)).fetchall()
        con.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        os.unlink(tmp)
    return [(h.decode(), n.decode(), ev, int(exp)) for (h, n, ev, exp) in rows
            if applies(h.decode(), host)]


# host -> Chrome profile the cookies were last read from
_profile_for = {}


def profile_for(host):
    return (pinned_profile() or _profile_for.get(host)
            or next(iter(_profile_for.values()), None) or "Default")


def cookies(host):
    """{name: value} for every cookie Chrome would send to `host` ({} if none)."""
    pinned = pinned_profile()
    profiles = [pinned] if pinned else list_profiles()
    best = None  # (count, max_expiry, profile, rows)
    for prof in profiles:
        db = os.path.join(user_data_dir(), prof, "Cookies")
        if not os.path.exists(db):
            continue
        rows = _read_rows(db, host)
        if rows:
            cand = (len(rows), max(r[3] for r in rows), prof, rows)
            if best is None or cand[:2] > best[:2]:
                best = cand
    if not best:
        return {}
    _, _, prof, rows = best
    _profile_for[host] = prof

    def rank(row):
        hk = row[0].lstrip(".")
        return (1 if hk == host else 0, len(hk), row[3])

    chosen = {}
    for row in rows:
        if row[1] not in chosen or rank(row) > rank(chosen[row[1]]):
            chosen[row[1]] = row
    keys = _build_keys()
    out = {}
    for name in sorted(chosen):
        val = _decrypt(keys, chosen[name][2])
        if val:
            out[name] = val
    return out


def cookie_header(host, only=None):
    jar = cookies(host)
    if only:
        jar = {k: v for k, v in jar.items() if k in only}
    return "; ".join(f"{k}={v}" for k, v in jar.items())


# ---------------------------------------------------------------- open chrome
def best_profile(host):
    """The profile holding the most cookies for `host`, without decrypting any.

    Counting rows needs no AES key, so choosing a profile never triggers the
    Keychain prompt. Falls back to profile_for() when no profile has cookies.
    """
    pinned = pinned_profile()
    if pinned:
        return pinned
    best, best_n = None, 0
    for prof in list_profiles():
        n = len(_read_rows(os.path.join(user_data_dir(), prof, "Cookies"), host))
        if n > best_n:
            best, best_n = prof, n
    return best or profile_for(host)


def open_url(url, host=None, profile=None):
    """Open `url` in the user's Chrome. Returns the profile used.

    The profile defaults to the one we read `host`'s cookies from (or the one
    holding most of them), so the page opens already logged in.
    """
    host = host or host_of(url)
    prof = profile or _profile_for.get(host) or best_profile(host)
    args = [f"--profile-directory={prof}"]
    if os.environ.get("CHROME_USER_DATA") or _settings["user_data"]:
        args.insert(0, f"--user-data-dir={user_data_dir()}")
    if IS_MAC:
        # `open -na` hands the URL to the running Chrome when there is one; the
        # profile flag picks the window. Plain `open -a` ignores --args.
        cmd = ["open", "-na", "Google Chrome", "--args"] + args + [url]
    else:
        exe = next((shutil.which(c) for c in ("google-chrome", "google-chrome-stable",
                                               "chromium", "chromium-browser") if shutil.which(c)), None)
        cmd = [exe] + args + [url] if exe else ["xdg-open", url]
    log("opening browser:", " ".join(cmd))
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        raise ToolError(f"could not open Chrome ({e}). Open {url} yourself.")
    return prof
