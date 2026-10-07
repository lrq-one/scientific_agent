import pytest

from app.services.demo_data import ensure_demo_data


@pytest.fixture(scope="session", autouse=True)
def demo_fixtures():
    ensure_demo_data()

