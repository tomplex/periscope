"""Tests for /api/projects/*.

The POST /api/projects (create) and POST /api/projects/pr-review routes
were retired in favour of POST /api/open. Their coverage lives in:
  - tests/routes/test_open.py   (route-level)
  - tests/test_open_ops.py      (function-level, incl. fetch_pr rollback)
"""
