"""
Wspolny interfejs brokera. Kazdy broker to osobny folder w invapp/brokers/<nazwa>/ z modulem broker.py, ktory definiuje klase dziedziczaca po Broker.
Reszta aplikacji (import z interfejsu, zakladki, ceny, wskazniki, kontrola liczb) korzysta tylko z tego interfejsu, wiec dodanie brokera
nie wymaga zmian w innych miejscach poza wpisem na liscie w invapp/brokers/__init__.py. Instrukcja: docs/DODAWANIE_BROKERA.md.

Zasady wspolne dla wszystkich brokerow (wynikaja z decyzji uzytkownika):
* import jest atomowy i idempotentny (ten sam plik albo nowszy eksport niczego nie dubluje; kolejnosc plikow nie ma znaczenia);
* surowe wiersze sa zapisywane, nieznane typy operacji = UNMAPPED (nie zgadujemy);
* koszt = gotowka z ksiegi (z oplatami); dane osobowe (IBAN, nazwiska) tylko w postaci skrotu/maski;
* waluta i gielda instrumentu nie wynikaja z sufiksu symbolu - tylko ze zrodla (specyfikacja brokera, regula uzytkownika, zrodlo zewnetrzne ze sprawdzeniem ceny).
"""


class Broker:
    code = None            # kod w bazie (financial_institutions.code), np. "XTB"
    key = None             # krotki klucz w adresie zakladki, np. "xtb"
    name = None            # nazwa w interfejsie, np. "XTB"
    file_help = ""         # opis plikow do wgrania (po polsku, HTML dozwolony)
    accept = ()            # rozszerzenia plikow, np. (".xlsx",)

    @property
    def scope_code(self):
        return f"BROKER:{self.code}"

    def matches(self, filename: str, head: bytes) -> bool:
        """Czy wgrany plik nalezy do tego brokera (nazwa i poczatek zawartosci)? Bez efektow ubocznych."""
        raise NotImplementedError

    def validate_upload(self, filename: str, data: bytes) -> None:
        """Walidacja wgranego pliku przed zapisem na dysk; blad = ImportFailed z komunikatem po polsku."""

    def import_path(self, conn, path: str, display_name: str = None) -> dict:
        """Import pliku (bez commitu) + przeliczenie warstwy pochodnej. Raport: file, counters, created_accounts, warnings, checks, info."""
        raise NotImplementedError

    def rebuild_derived(self, conn) -> dict:
        """Przeliczenie partii i zamkniec od zera z ksiegi tego brokera."""
        raise NotImplementedError

    def pricing_steps(self, conn, provider):
        """Kroki zadania w tle: lista (tytul, funkcja bez argumentow, faza). Fazy: "quick" (przed pierwszym pobraniem notowan, np. powiazanie
        symboli), "slow" (np. specyfikacje do pobrania; po nich notowania sa pobierane ponownie), "history" (przed pobraniem historii cen).
        Krotka bez fazy = "slow"."""
        return []

    def pending_questions(self, conn):
        """Pytania do uzytkownika wymagane przez tego brokera (np. potwierdzenie waluty), lista slownikow {id, tytul, tresc, odpowiedzi[, pola]}."""
        return []

    def answer_question(self, conn, question_id: str, answer: str, fields: dict = None) -> dict:
        """Odpowiedz uzytkownika (wartosc przycisku) i ewentualne pola formularza pytania (np. {"data": "2025-07-31", "zrodlo": "..."})."""
        raise NotImplementedError
