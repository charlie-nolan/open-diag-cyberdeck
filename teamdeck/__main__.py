"""python -m teamdeck [--install-icon]"""
import sys

from .gui import main

sys.exit(main(sys.argv[1:]))
