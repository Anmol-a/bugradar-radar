import sys

from radar.cli import main

if __name__ == "__main__":     # guard: bench worker processes (spawn on macOS) re-import this module
    sys.exit(main())
