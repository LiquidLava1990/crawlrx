"""Regression: primp (Rust) exceptions must be caught as network failures.

primp.TimeoutError derives from primp.PrimpError, not TimeoutError/OSError, so
without explicit registration it escapes every fetch tier and surfaces as an
unhandled HTTP 500 instead of a controlled timeout error (caught live:
scraping a hung fixture returned 500 instead of error_type=timeout).
"""
from __future__ import annotations

import pytest

primp = pytest.importorskip("primp")

from crawlrx._scraper_helpers import network_exception_types  # noqa: E402


def test_primp_errors_registered_as_network_exceptions() -> None:
    types = network_exception_types()
    assert primp.PrimpError in types, "primp.PrimpError must be a registered network exception"


def test_primp_timeout_is_caught_by_fetch_failure_tuple() -> None:
    from crawlrx.smart_fetch import _FETCH_FAILURE_EXCEPTIONS

    assert issubclass(primp.TimeoutError, _FETCH_FAILURE_EXCEPTIONS)


def test_primp_timeout_subclasses_exception_not_oserror() -> None:
    # Guard the premise: primp.TimeoutError would NOT be caught by OSError/TimeoutError
    assert issubclass(primp.TimeoutError, Exception)
    assert not issubclass(primp.TimeoutError, OSError)
