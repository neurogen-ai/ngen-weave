"""Smoke test: the server package imports."""


def test_package_imports():
    import ngen_weave_server

    assert ngen_weave_server is not None
