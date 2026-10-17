# Reuse the throwaway Postgres cluster fixtures from the store_pg package.
from tests.runtime.store_pg.conftest import clock, event, pg_dsn, pg_server, spec  # noqa: F401


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: slow tests (docker build)")
