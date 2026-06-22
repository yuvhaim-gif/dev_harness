"""Agent workflow harness.

Marks ``harness`` as an importable package so it can be installed and exposed via
console scripts. The orchestrator and its siblings use flat, script-mode imports
(each inserts this directory on ``sys.path`` at import time), so this file
intentionally adds nothing else.
"""
