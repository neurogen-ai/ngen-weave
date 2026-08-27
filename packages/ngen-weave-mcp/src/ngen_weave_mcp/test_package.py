"""Smoke test: the MCP package imports."""


def test_package_imports():
    import ngen_weave_mcp

    assert ngen_weave_mcp is not None
