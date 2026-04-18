import sys
import os

# Ensure the project root is on sys.path so claude_cli_proxy is importable
# without requiring a pip install.
sys.path.insert(0, os.path.dirname(__file__))
