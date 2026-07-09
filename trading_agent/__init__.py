from pathlib import Path


_SRC_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "trading_agent"
if _SRC_PACKAGE.exists():
    __path__.insert(0, str(_SRC_PACKAGE))  # type: ignore[name-defined]

__all__ = ["__version__"]
__version__ = "0.1.0"
