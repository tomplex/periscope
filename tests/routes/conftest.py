"""Shared fixtures for route tests.

pytest auto-discovers conftest.py fixtures for every test module in this
directory, so route-test files just request `client` as a parameter.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)
