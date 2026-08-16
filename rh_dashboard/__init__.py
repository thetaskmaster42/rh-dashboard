"""
rh-dashboard — turn a folder of Robinhood statement CSVs into one dashboard.

Public API:

    from rh_dashboard import build_dashboard
    result = build_dashboard(input_dir="input", output_dir="output")

Everything else is an implementation detail.
"""
__version__ = "1.0.0"

from .loader import LoadError
from .pipeline import build_dashboard

__all__ = ["build_dashboard", "LoadError", "__version__"]
