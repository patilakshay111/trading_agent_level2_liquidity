from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "launcher.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


try:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write("Starting Trading Agent launcher\n")
    import run

    run.main()
except Exception:
    with LOG.open("a", encoding="utf-8") as handle:
        traceback.print_exc(file=handle)
    raise
