import multiprocessing

import pytest


@pytest.fixture(scope="session", autouse=True)
def reader_processes_stop():
    """The reader pool's children must not outlive the suite."""
    yield
    from handler.runtime import documents

    documents.shutdown_readers()
    assert multiprocessing.active_children() == []
