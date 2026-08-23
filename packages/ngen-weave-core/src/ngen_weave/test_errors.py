"""Tests for the error taxonomy."""

import pytest

from ngen_weave.errors import ConfigError, DataError, InfraError, NgWeaveError


def test_all_derive_from_root():
    assert issubclass(ConfigError, NgWeaveError)
    assert issubclass(DataError, NgWeaveError)
    assert issubclass(InfraError, NgWeaveError)
    assert issubclass(NgWeaveError, Exception)


def test_catchable_as_root():
    with pytest.raises(NgWeaveError):
        raise DataError("bad output")
    with pytest.raises(NgWeaveError):
        raise InfraError("timeout")


def test_message_preserved():
    err = ConfigError("unknown workflow: examples.x.Y")
    assert "unknown workflow" in str(err)


def test_taxonomy_classes_are_distinct():
    assert not issubclass(DataError, ConfigError)
    assert not issubclass(InfraError, DataError)
    assert not issubclass(InfraError, ConfigError)
