import json
import unittest
from unittest.mock import MagicMock, patch

from resourceflow.recent_air_ai import request_recent_air_dates


class RecentAirAIClientTests(unittest.TestCase):
    def test_openai_compatible_request_uses_configured_endpoint_and_secret_header(self):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "[]"}}],
        }).encode()
        response.__enter__.return_value = response

        with patch("resourceflow.recent_air_ai.urlopen", return_value=response) as urlopen:
            content = request_recent_air_dates(
                "https://ai.example/v1",
                "test-model",
                "private-key",
                "system",
                '{"episode": 1}',
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ai.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][0]["content"], "system")
        self.assertEqual(content, "[]")

    def test_api_http_errors_do_not_expose_response_body(self):
        from urllib.error import HTTPError

        error = HTTPError(
            "https://ai.example/v1/chat/completions", 401, "unauthorized",
            {}, None,
        )
        with patch("resourceflow.recent_air_ai.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
                request_recent_air_dates(
                    "https://ai.example/v1", "model", "secret", "system", "user",
                )
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
