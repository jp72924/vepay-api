"""Compatibility shim for the former VEPay OCR API module name."""

from vepay_api import *  # noqa: F401,F403
from vepay_api import main


if __name__ == "__main__":
    main()
