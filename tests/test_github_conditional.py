from __future__ import annotations

import io
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from reposteward.core.config import GitHubConfig
from reposteward.github.client import GitHubClient, GitHubReadError


class ConditionalTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient(
            GitHubConfig(login="owner"), token="test-only-placeholder"
        )

    def test_request_is_bounded_conditional_nonredirecting_and_closes(self):
        response = Mock(
            status=200,
            headers={
                "ETag": '"next"',
                "Link": '<https://api.github.com/next>; rel="next"',
            },
        )
        response.read.return_value = b'[{"number":1}]'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener):
            result = self.client.conditional_get(
                "/repos/owner/repo/pulls", query={"page": 1}, etag='"old"'
            )
        response.read.assert_called_once_with(2_000_001)
        response.__exit__.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("If-none-match"), '"old"')
        self.assertTrue(result.has_more)

    def test_304_and_rate_limit_close_the_error_stream(self):
        for code in (304, 429):
            headers = Message()
            headers["Retry-After"] = "120"
            stream = io.BytesIO(b"do not expose remote errors")
            error = urllib.error.HTTPError(
                "https://api.github.com/test", code, "error", headers, stream
            )
            opener = Mock()
            opener.open.side_effect = error
            with patch("urllib.request.build_opener", return_value=opener):
                if code == 304:
                    self.assertTrue(
                        self.client.conditional_get("/user", etag='"old"').not_modified
                    )
                else:
                    with self.assertRaises(GitHubReadError) as raised:
                        self.client.conditional_get("/user")
                    self.assertEqual(raised.exception.code, "rate_limited")
                    self.assertTrue(raised.exception.retry_at)
            self.assertTrue(stream.closed)

    def test_paths_and_headers_cannot_redirect_credentials(self):
        with patch(
            "urllib.request.build_opener", side_effect=AssertionError("network")
        ):
            for path in ("//evil.test/path", "/../user", "/repos/name?target=evil"):
                with self.assertRaises(ValueError):
                    self.client.conditional_get(path)
            with self.assertRaises(ValueError):
                self.client.conditional_get("/user", etag="x\r\ninjected: value")

    def test_repository_redirect_is_only_a_validated_same_api_hint(self):
        for location, allowed in [
            ("https://api.github.com/repositories/123", True),
            ("/repos/owner/renamed", True),
            ("https://evil.test/repositories/123", False),
            ("https://api.github.com.evil.test/repositories/123", False),
            ("https://api.github.com/repos/owner/repo?token=secret", False),
            ("https://api.github.com/user", False),
        ]:
            stream = io.BytesIO(b"do not expose redirect body")
            headers = Message()
            headers["Location"] = location
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://api.github.com/repos/owner/repo", 301, "moved", headers, stream
            )
            with (
                patch("urllib.request.build_opener", return_value=opener),
                self.assertRaises(GitHubReadError) as raised,
            ):
                self.client.conditional_get("/repos/owner/repo")
            self.assertEqual(raised.exception.code == "repository_moved", allowed)
            self.assertEqual(opener.open.call_count, 1)
            self.assertTrue(stream.closed)
            self.assertNotIn("secret", str(raised.exception))
