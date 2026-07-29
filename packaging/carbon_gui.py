"""PyInstaller entry point for the window.

A file of its own because PyInstaller freezes a *script*, and freezing
`gui/app.py` directly would import it as `__main__` rather than as
`carbon_scraper.gui.app` — two module objects for one file, and the `from ..
import db` relative imports would fail.

Nothing belongs here but the call. `carbon-gui` (the console-script launcher
used from a checkout) and the packaged EXE must run the same `main()`, or the
thing shipped to the business is not the thing that was tested.
"""

from __future__ import annotations

from carbon_scraper.gui.app import main

if __name__ == "__main__":
    main()
