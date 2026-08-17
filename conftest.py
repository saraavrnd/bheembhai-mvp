"""Repo-root conftest — keeps stray scratch scripts out of pytest collection.

``quick_test.py`` matches pytest's ``*_test.py`` glob and mutates ``os.environ``
(DEV_AUTH_BYPASS, DATABASE_URL, …) at import time, which would poison the whole
session's environment for every other test file. It is a dev scratch script,
not a test suite — exclude it from collection.

``bb-workdir/`` holds agent-run clones of arbitrary repos (including their own
``tests/`` trees) — never part of this project's suite, and actively changing
while runs are in flight.
"""

collect_ignore = ["quick_test.py", "main.py", "bb-workdir", "infra/bb-workdir"]
