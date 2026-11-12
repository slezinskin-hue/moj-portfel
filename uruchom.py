"""Uruchamia aplikacje z folderu zrodlowego (to samo co: python -m invapp). Skrot na pulpicie moze wskazywac ten plik."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from invapp.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
