import config
from keybroker import dialect

def test_suite_can_never_reach_production_postgres():
    """Regression guard: a real SUPABASE_DB_URL in .env must not make the
    suite write to the live project. This failed once for real."""
    assert config.SUPABASE_DB_URL == ""
    assert dialect.active().name == "sqlite"
