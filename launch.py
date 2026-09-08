"""Entry point for launching the overlay by file path.

`python -m app.main` needs the project directory to be the working directory.
Shortcuts and the Windows Run key do not guarantee that, so this script puts the
project root on `sys.path` itself and can be double-clicked from anywhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402 (path setup must come first)

if __name__ == "__main__":
    raise SystemExit(main())
