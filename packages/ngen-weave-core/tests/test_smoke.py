"""Smoke test: the cli entry point imports."""

def test_cli_entry_point_imports():
    from ngen_weave_cli.main import app

    assert app is not None
