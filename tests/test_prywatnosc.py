"""
Prywatnosc (decyzja uzytkownika 2026-11-12: "nikt nie udostepni nigdzie danych swoich przez przypadek"):
* repozytorium nie zawiera plikow z danymi (eksporty, bazy, kopie) ani numerow kont bankowych;
* dane uzytkownika sa poza katalogiem programu, a instalator nie pakuje danych;
* do internetu wysylane sa tylko symbole instrumentow (Yahoo), zapytania o kursy (NBP) i o wydania (GitHub) - bez danych portfela.
"""
import os, re, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA_EXT = (".xlsx", ".xls", ".csv", ".db", ".sqlite", ".sqlite3", ".dump", ".backup", ".bak", ".pdf")


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        return [f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        raise unittest.SkipTest("brak gita – test tylko w repozytorium")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore") as f:
        return f.read()


class TestPrivacy(unittest.TestCase):
    def test_no_data_files_in_repository(self):
        bad = [f for f in tracked_files() if f.lower().endswith(DATA_EXT) or f.startswith(("export/", "lokalne/", "build/", "dist/"))
               or "hasla" in f.lower() or f.lower().endswith(("stan.json",))]
        self.assertEqual(bad, [], "pliki z danymi w repozytorium")

    def test_no_bank_account_numbers_or_emails_in_code(self):
        iban = re.compile(r"\b(PL)?\d{2}[ ]?(\d{4}[ ]?){6}\b")                       # numer rachunku (NRB/IBAN), takze z odstepami
        mail = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")
        hits = []
        for f in tracked_files():
            if not f.endswith((".py", ".js", ".html", ".css", ".sql", ".md", ".txt", ".iss", ".ps1", ".yml", ".toml", ".json")):
                continue
            text = read(f)
            for m in iban.finditer(text):
                hits.append(f"{f}: {m.group(0)[:6]}…")
            for m in mail.finditer(text):
                if not m.group(0).endswith(("@example.com", "@example.org", "@users.noreply.github.com")) and not m.group(0).startswith(("git@", "noreply@")):
                    hits.append(f"{f}: adres e-mail")
        self.assertEqual(hits, [])

    def test_personal_tests_and_exports_are_ignored(self):
        gi = read(".gitignore")
        for p in ("export/", "lokalne/", "tests/test_lokalne.py", "tests/fixtures/", "build/", "dist/", "invapp/_build.py"):
            self.assertIn(p, gi)

    def test_data_dir_outside_program_and_not_packaged(self):
        from invapp import config
        prog = os.path.realpath(ROOT)
        self.assertFalse(os.path.realpath(str(config.installed_data_dir())).startswith(prog))
        iss = read(os.path.join("instalator", "moj-portfel.iss"))
        files = [l for l in iss.splitlines() if l.strip().startswith("Source:")]
        self.assertTrue(files)
        for l in files:
            self.assertNotRegex(l, r"(?i)export|AplikacjaInwestycyjna|localappdata|kopie|baza")
        build = read(os.path.join("instalator", "build.ps1"))
        self.assertNotRegex(build, r"(?i)robocopy[^\n]*export")                       # eksporty nie trafiaja do instalatora
        self.assertIn("CurUninstallStepChanged", iss)                                    # deinstalacja pyta o usuniecie danych (domyslnie: nie)

    def test_outgoing_requests_only_to_known_services(self):
        """Wszystkie adresy w kodzie programu: tylko Yahoo (ceny), NBP (kursy), XTB (specyfikacja instrumentow), GitHub (aktualizacje)."""
        allowed = ("query1.finance.yahoo.com", "query2.finance.yahoo.com", "api.nbp.pl", "api.github.com", "github.com", "www.xtb.com", "xtb.com", "127.0.0.1", "www.w3.org")
        hosts = set()
        for f in tracked_files():
            if f.startswith("invapp/") and f.endswith((".py", ".js", ".html")):
                hosts |= set(re.findall(r"https?://([A-Za-z0-9.-]+)", read(f)))
        self.assertEqual(sorted(h for h in hosts if h not in allowed and not h.endswith(".xtb.com")), [])


if __name__ == "__main__":
    unittest.main()
