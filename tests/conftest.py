import os
import sys

# Make the project root (which holds strava_tools.py) importable no matter
# which directory pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))