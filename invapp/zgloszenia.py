"""
Zgloszenie bledu (decyzja wlasciciela 2026-11-20): uzytkownik opisuje blad w aplikacji, a aplikacja przygotowuje gotowe zgloszenie
(issue) w repozytorium na GitHubie - wlasciciel repozytorium dostaje powiadomienie GitHuba.

Zasady (prywatnosc - "nikt nie udostepni danych przez przypadek"):
* aplikacja NICZEGO nie wysyla sama: buduje tylko adres https://github.com/{repo}/issues/new?title=...&body=... ; zgloszenie otwiera
  i zatwierdza uzytkownik w przegladarce (na swoim koncie GitHub), widzac wczesniej dokladna tresc;
* tresc = TYLKO to, co wpisal uzytkownik + (opcjonalnie) wersja aplikacji, system, tryb okna i zakladka - zadnych kwot, pozycji, kont, plikow;
* w wpisanym tekscie automatycznie maskowane: adresy e-mail, dlugie numery (rachunki/IBAN, PESEL - 11 i wiecej cyfr), sciezki
  z nazwa uzytkownika Windows (C:\\Users\\<nazwa>) - z informacja, co zamaskowano;
* bez klucza/tokenu GitHuba w aplikacji (publiczny program - klucz moglby wyciagnac kazdy);
* repozytorium jak dla aktualizacji (updates.build_info); brak repozytorium -> tylko tresc do skopiowania.
"""
import platform, re, urllib.parse

from invapp import __version__

LIMITS = {"tytul": 120, "opis": 4000, "kroki": 2000, "oczekiwane": 1000}
MAX_URL = 7000                       # GitHub przyjmuje adresy do ok. 8 KB
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONGNUM = re.compile(r"(?<![\w])(?:[A-Z]{2}\s?)?\d(?:[\s-]?\d){10,}(?![\w])")      # 11+ cyfr (PESEL, rachunek, IBAN; z odstepami)
_USERPATH = re.compile(r"([A-Za-z]:[\\/]+(?:Users|Użytkownicy|Uzytkownicy)[\\/]+)[^\\/\s\"'<>|]+", re.IGNORECASE)
_VIEW = re.compile(r"^[a-z0-9-]{0,24}$")


def system_name():
    """Np. "Windows 10.0.26300" (bez nazwy komputera i uzytkownika)."""
    try:
        if platform.system() == "Windows":
            return "Windows " + platform.version()
        return f"{platform.system()} {platform.release()}"
    except Exception:  # noqa: BLE001
        return "nieznany"


def mask(text):
    """(tekst z zamaskowanymi danymi osobowymi, lista tego, co zamaskowano)."""
    found = []
    text, n = _USERPATH.subn(lambda m: m.group(1) + "<użytkownik>", text)
    if n:
        found.append("ścieżka z nazwą użytkownika Windows")
    text, n = _EMAIL.subn("<e-mail usunięty>", text)
    if n:
        found.append("adres e-mail")
    text, n = _LONGNUM.subn("<numer usunięty>", text)
    if n:
        found.append("długi numer (rachunek, IBAN, PESEL…)")
    return text, found


def _clean(value, limit):
    s = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return s[:limit]


def build(fields, repo, tech=None):
    """fields: {tytul, opis, kroki, oczekiwane}; tech: {"tryb", "widok"} albo None (bez informacji technicznych).
    Zwraca {"tytul", "tresc", "url" (albo None bez repozytorium), "zamaskowano": [...], "bledy": [...], "skrocono": bool}."""
    f = {k: _clean(fields.get(k), lim) for k, lim in LIMITS.items()}
    masked = []
    for k in f:
        f[k], found = mask(f[k])
        masked += [x for x in found if x not in masked]
    errors = []
    if not f["tytul"]:
        errors.append("Wpisz krótki tytuł.")
    if not f["opis"]:
        errors.append("Opisz, co się stało.")
    title = "[Błąd] " + f["tytul"]

    CUT = "\n\n… (skrócono – pełny tekst za długi na zgłoszenie z aplikacji)"

    def body(t):
        parts = ["### Co się stało\n" + (t["opis"] or "_(brak)_")]
        if t["kroki"]:
            parts.append("### Jak to powtórzyć\n" + t["kroki"])
        if t["oczekiwane"]:
            parts.append("### Czego się spodziewałem\n" + t["oczekiwane"])
        if tech is not None:
            view = str(tech.get("widok") or "")
            mode = {"okno": "okno aplikacji", "przegladarka": "przeglądarka"}.get(tech.get("tryb"), "nieznany")
            lines = [f"- Wersja: {__version__}", f"- System: {system_name()}", f"- Tryb: {mode}"]
            if view and _VIEW.match(view):
                lines.append(f"- Zakładka: {view}")
            parts.append("### Informacje techniczne\n" + "\n".join(lines))
        parts.append("_Zgłoszenie przygotowane w aplikacji Mój Portfel (bez danych portfela)._")
        return "\n\n".join(parts)

    text, short = body(f), False
    url = None
    if repo and _REPO.match(repo):
        def make(t):
            return f"https://github.com/{repo}/issues/new?" + urllib.parse.urlencode({"title": title, "body": t, "labels": "bug"}, quote_via=urllib.parse.quote)
        url = make(text)
        cut = {k: f[k] for k in ("opis", "kroki", "oczekiwane")}
        while len(url) > MAX_URL:                                  # za dlugo na adres: skracamy najdluzsze pole (uzytkownik widzi wynik)
            k = max(cut, key=lambda x: len(cut[x]))
            if not cut[k]:
                break
            cut[k] = cut[k][: max(0, int(len(cut[k]) * 0.8) - 20)]
            text, short = body({x: (v + CUT if v != f[x] else v) for x, v in cut.items()}), True
            url = make(text)
    return {"tytul": title, "tresc": text, "url": url if not errors else None, "zamaskowano": masked, "bledy": errors, "skrocono": short}
