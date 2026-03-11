import sys
from cli import main

with open("sync_output.txt", "w", encoding="utf-8") as f:
    sys.stdout = f
    sys.stderr = f
    try:
        sys.argv = ["cli.py", "--verbose"]
        main()
    except SystemExit:
        pass
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
