import config
from keybroker import dialect

def test_suite_can_never_reach_production_postgres():
    """Regression guard: a real SUPABASE_DB_URL in .env must not make the
    suite write to the live project. This failed once for real."""
    assert config.SUPABASE_DB_URL == ""
    assert dialect.active().name == "sqlite"


class _FakePostgresDialect:
    """Stands in for a PostgresDialect left cached by a fixture that died
    before its teardown. It must never survive into the next test."""

    name = "postgres"

    def close_pool(self) -> None:
        pass


def test_a_poisons_the_dialect_cache():
    # Deliberately leaves module-global state behind, exactly as a contract-test
    # fixture does when init_db() raises before its yield.
    dialect._active = _FakePostgresDialect()


def test_b_autouse_guard_reset_the_poisoned_cache():
    """Regression guard for the real hole: blanking config.SUPABASE_DB_URL
    protects nothing when dialect._active already holds a Postgres dialect,
    because active() consults config only when the cache is empty. Before the
    fix this test saw 'postgres' and every following test in the session would
    have run against the network database."""
    assert dialect.active().name == "sqlite"
