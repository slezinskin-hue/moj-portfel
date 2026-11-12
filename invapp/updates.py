"""
Aktualizacje z wydan (Releases) repozytorium na GitHubie (decyzja uzytkownika 2026-11-12: powiadomienie + instalacja jednym kliknieciem).

* Repozytorium pochodzi z pliku invapp/_build.py, ktory tworzy budowanie instalatora (GitHub Actions wpisuje nazwe swojego repozytorium).
  Wersja uruchamiana ze zrodel (bez _build.py) nie sprawdza aktualizacji, chyba ze ustawiono INVAPP_UPDATE_REPO (np. "uzytkownik/moj-portfel").
* Sprawdzenie: GET https://api.github.com/repos/{repo}/releases/latest (publiczne API, bez logowania), tag vX.Y.Z, plik instalatora (.exe)
  i jego suma SHA-256 (z opisu wydania "SHA256: ..." albo z pola digest pliku). Wynik pamietany 6 godzin (limit API: 60 zapytan/h).
* Instalacja: pobranie instalatora TYLKO z adresu wydania tego repozytorium (https://github.com/{repo}/releases/download/...), sprawdzenie SHA-256,
  uruchomienie w trybie cichym. Instalator sam zatrzymuje aplikacje, instaluje nowa wersje na starej i ja uruchamia; dane uzytkownika zostaja,
  migracje robia kopie zapasowa.
"""
import hashlib, json, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request

from invapp import __version__, config

API = "https://api.github.com"
CHECK_TTL = 6 * 3600
_cache = {"at": 0.0, "data": None, "release": None}
_lock = threading.Lock()


def build_info():
    try:
        from invapp import _build
        return {"repo": getattr(_build, "UPDATE_REPO", "")}
    except ImportError:
        return {"repo": os.environ.get("INVAPP_UPDATE_REPO", "")}


def parse_version(s):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "moj-portfel-aktualizacje", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def latest_release(info, fetch=_get_json):
    """Najnowsze wydanie: {version, tag, name, notes, installer_url, installer_name, sha256, page} albo None."""
    r = fetch(f"{API}/repos/{info['repo']}/releases/latest")
    v = parse_version(r.get("tag_name"))
    if not v:
        return None
    exe = next((a for a in r.get("assets") or [] if (a.get("name") or "").lower().endswith(".exe")), None)
    sha = re.search(r"SHA256:\s*([0-9a-fA-F]{64})", r.get("body") or "")
    digest = ((exe or {}).get("digest") or "")
    sha256 = sha.group(1).lower() if sha else (digest[7:].lower() if digest.startswith("sha256:") else None)
    return {"version": ".".join(map(str, v)), "tag": r.get("tag_name"), "name": r.get("name"), "notes": (r.get("body") or "")[:4000],
            "installer_url": (exe or {}).get("browser_download_url"), "installer_name": (exe or {}).get("name"),
            "sha256": sha256, "page": r.get("html_url")}


def check(force=False, fetch=_get_json):
    """Stan aktualizacji dla strony. Nigdy nie rzuca wyjatku (brak internetu = brak informacji)."""
    info = build_info()
    out = {"wersja": __version__, "repozytorium": info["repo"] or None, "dostepna": False}
    if not info["repo"]:
        out["powod"] = "Sprawdzanie aktualizacji jest wyłączone w wersji uruchamianej ze źródeł."
        return out
    with _lock:
        if not force and _cache["data"] is not None and time.time() - _cache["at"] < CHECK_TTL:
            return {**out, **_cache["data"]}
    try:
        rel = latest_release(info, fetch)
    except Exception as e:  # noqa: BLE001
        return {**out, "powod": f"Nie udało się sprawdzić aktualizacji ({type(e).__name__})."}
    data = {"najnowsza": rel["version"] if rel else None, "notatki": rel["notes"] if rel else "", "strona": rel["page"] if rel else None}
    if rel and parse_version(rel["version"]) > parse_version(__version__) and rel["installer_url"] and rel["sha256"]:
        data.update(dostepna=True, instalator=rel["installer_name"])
    with _lock:
        _cache.update(at=time.time(), data=data, release=rel)
    return {**out, **data}


def _allowed(url, repo):
    """Tylko plik z wydania TEGO repozytorium (https://github.com/{repo}/releases/download/...)."""
    u = urllib.parse.urlparse(url or "")
    return u.scheme == "https" and u.hostname == "github.com" and u.path.lower().startswith(f"/{repo.lower()}/releases/download/")


def download_and_verify(rel, repo, dest_dir, fetch_bytes=None):
    """Pobiera instalator (tylko z wydania repozytorium) i sprawdza SHA-256. Zwraca sciezke pliku."""
    if not _allowed(rel["installer_url"], repo):
        raise ValueError("Adres instalatora nie pochodzi z wydania tego repozytorium – przerwano.")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", rel["installer_name"] or "instalator.exe")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    if fetch_bytes is None:
        req = urllib.request.Request(rel["installer_url"], headers={"User-Agent": "moj-portfel-aktualizacje"})
        with urllib.request.urlopen(req, timeout=300) as r:          # GitHub przekierowuje do swojego serwera plikow (https)
            data = r.read()
    else:
        data = fetch_bytes(rel["installer_url"])
    if hashlib.sha256(data).hexdigest() != rel["sha256"]:
        raise ValueError("Suma kontrolna pobranego instalatora się nie zgadza – przerwano (plik nie został zapisany).")
    path.write_bytes(data)
    return path


def install_latest():
    """Pobiera i uruchamia instalator najnowszej wersji (tryb cichy). Instalator zatrzyma aplikacje i uruchomi nowa wersje."""
    st = check(force=True)
    if not st.get("dostepna"):
        raise ValueError(st.get("powod") or "Brak nowszej wersji.")
    rel = _cache.get("release")
    path = download_and_verify(rel, build_info()["repo"], config.data_dir() / "aktualizacje")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"uruchomiono": True, "wersja": rel["version"], "plik": path.name}


if __name__ == "__main__":
    print(json.dumps(check(force=True), ensure_ascii=False, indent=1))
    sys.exit(0)
