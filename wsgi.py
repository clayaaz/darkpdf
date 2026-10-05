"""WSGI entry point for PythonAnywhere.

In the PythonAnywhere "Web" tab, set the WSGI configuration file to:

    import sys
    sys.path.insert(0, '/home/YOUR_USERNAME/darkpdf')
    from wsgi import application

or point it at this file and keep the path edit below in sync.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as application  # noqa: E402,F401
