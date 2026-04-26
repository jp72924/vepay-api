#!/usr/bin/env python3
"""Compatibility shim for the former VEPay OCR module name."""

from vepay_api_core import *  # noqa: F401,F403
from vepay_api_core import main


if __name__ == "__main__":
    raise SystemExit(main())
