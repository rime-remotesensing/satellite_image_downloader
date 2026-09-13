from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

# Load network_retry.py directly by file path rather than importing the `src`
# package, since src/__init__.py pulls in optional heavy dependencies
# (planetary_computer, omnicloudmask, ...) that this unit has no need for.
_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "network_retry.py"
_spec = importlib.util.spec_from_file_location("network_retry", _MODULE_PATH)
network_retry = importlib.util.module_from_spec(_spec)
sys.modules["network_retry"] = network_retry
_spec.loader.exec_module(network_retry)
call_with_network_retry = network_retry.call_with_network_retry


def _make_http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code} error", response=response)


class TestTransientNetworkFailureRetries:
    def test_dns_failure_three_times_then_success_returns_result(self):
        """gaierror-style DNS failures: retried, same call eventually succeeds."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise requests.exceptions.ConnectionError(
                    socket.gaierror(-2, "Name or service not known")
                )
            return "success-payload"

        with patch("network_retry.time.sleep") as mock_sleep:
            result = call_with_network_retry(flaky, service="CMR")

        assert result == "success-payload"
        assert calls["n"] == 4
        assert mock_sleep.call_count == 3
        # backoff schedule: 5s, 10s, 20s for attempts 1, 2, 3
        assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 10, 20]

    def test_many_dns_failures_keeps_retrying_without_skip_or_exit(self):
        """No matter how many times it fails, it must never give up / raise."""
        calls = {"n": 0}
        attempts_to_simulate = 50

        def always_fails():
            calls["n"] += 1
            if calls["n"] >= attempts_to_simulate:
                return "finally-success"
            raise requests.exceptions.ConnectionError(
                socket.gaierror(-2, "Name or service not known")
            )

        with patch("network_retry.time.sleep") as mock_sleep:
            result = call_with_network_retry(always_fails, service="FIRMS")

        assert result == "finally-success"
        assert calls["n"] == attempts_to_simulate
        # backoff caps at 60s and never raises/aborts regardless of attempt count
        assert mock_sleep.call_args_list[-1].args[0] == 60
        assert all(c.args[0] <= 60 for c in mock_sleep.call_args_list)

    def test_timeout_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.Timeout("Read timed out")
            return "ok"

        with patch("network_retry.time.sleep"):
            assert call_with_network_retry(flaky, service="CMR") == "ok"
        assert calls["n"] == 2

    def test_http_5xx_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _make_http_error(503)
            return "ok"

        with patch("network_retry.time.sleep"):
            assert call_with_network_retry(flaky, service="FIRMS") == "ok"
        assert calls["n"] == 2

    def test_wrapped_dns_failure_via_exception_chain_is_retried(self):
        """earthaccess/CMR code often wraps the underlying error in a RuntimeError
        via `raise ... from exc`; the classifier must look at __cause__."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                try:
                    raise requests.exceptions.ConnectionError(
                        socket.gaierror(-2, "Name or service not known")
                    )
                except requests.exceptions.ConnectionError as exc:
                    raise RuntimeError("CMR search failed") from exc
            return "ok"

        with patch("network_retry.time.sleep"):
            assert call_with_network_retry(flaky, service="CMR") == "ok"
        assert calls["n"] == 2


class TestExplicitErrorsAreNotRetried:
    def test_http_401_raises_immediately_no_infinite_retry(self):
        calls = {"n": 0}

        def unauthorized():
            calls["n"] += 1
            raise _make_http_error(401)

        with patch("network_retry.time.sleep") as mock_sleep:
            with pytest.raises(requests.HTTPError):
                call_with_network_retry(unauthorized, service="CMR")

        assert calls["n"] == 1
        mock_sleep.assert_not_called()

    def test_http_403_raises_immediately(self):
        calls = {"n": 0}

        def forbidden():
            calls["n"] += 1
            raise _make_http_error(403)

        with patch("network_retry.time.sleep") as mock_sleep:
            with pytest.raises(requests.HTTPError):
                call_with_network_retry(forbidden, service="FIRMS")

        assert calls["n"] == 1
        mock_sleep.assert_not_called()

    def test_malformed_request_value_error_raises_immediately(self):
        def programming_error():
            raise ValueError("Invalid bounding box")

        with patch("network_retry.time.sleep") as mock_sleep:
            with pytest.raises(ValueError):
                call_with_network_retry(programming_error, service="CMR")

        mock_sleep.assert_not_called()

    def test_firms_style_wrapped_401_raises_immediately(self):
        """activefire._fetch_firms_rows wraps HTTPError into RuntimeError via
        `raise ... from exc`; the classifier must see the chained status code
        and still refuse to retry an auth failure."""

        def bad_api_key():
            try:
                raise _make_http_error(401)
            except requests.HTTPError as exc:
                raise RuntimeError("FIRMS request failed (401) ...") from exc

        with patch("network_retry.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError):
                call_with_network_retry(bad_api_key, service="FIRMS")

        mock_sleep.assert_not_called()
