import pytest

from ticket.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")
