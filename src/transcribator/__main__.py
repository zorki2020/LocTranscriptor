"""`python -m transcribator`: how bench runs processing in a child process."""

import sys

from transcribator.cli import main

if __name__ == "__main__":
    sys.exit(main())
