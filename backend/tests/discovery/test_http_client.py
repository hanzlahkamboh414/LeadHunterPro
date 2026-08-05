"""Tests for HTTPClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.engines.source_connectors.http_client import HTTPClient


class TestHTTPClient:

    def test_context_manager(self):
        with HTTPClient(timeout=5, rate_limit=0.0) as client:
            assert client._timeout == 5

    def test_close(self):
        client = HTTPClient(rate_limit=0.0)
        client.close()  # should not raise

    @patch("app.engines.source_connectors.http_client.requests.Session")
    def test_get_success(self, mock_session_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        with HTTPClient(rate_limit=0.0) as client:
            resp = client.get("https://example.com/api")

        assert resp == mock_resp
        mock_session.get.assert_called_once()

    @patch("app.engines.source_connectors.http_client.requests.Session")
    def test_get_raises_on_http_error(self, mock_session_cls):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("404")
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        with HTTPClient(rate_limit=0.0) as client:
            with pytest.raises(requests.HTTPError):
                client.get("https://example.com/missing")

    @patch("app.engines.source_connectors.http_client.requests.Session")
    def test_post_success(self, mock_session_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        with HTTPClient(rate_limit=0.0) as client:
            resp = client.post("https://example.com/api", json={"key": "val"})

        assert resp == mock_resp

    def test_default_timeout(self):
        client = HTTPClient(rate_limit=0.0)
        assert client._timeout == 30

    def test_custom_user_agent(self):
        client = HTTPClient(user_agent="TestAgent/1.0", rate_limit=0.0)
        assert client._user_agent == "TestAgent/1.0"
