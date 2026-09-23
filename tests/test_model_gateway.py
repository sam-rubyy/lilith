import unittest
from unittest.mock import patch

from lilith.model_gateway import OllamaGateway


class GatewayTests(unittest.TestCase):
    def test_schema_and_deterministic_options_are_sent(self):
        gateway = OllamaGateway("fake")
        schema = {"type": "object", "properties": {"approved": {"type": "boolean"}}}
        with patch.object(gateway, "_request", return_value={"message": {"content": '{"approved": true}'}}) as request:
            self.assertEqual(gateway.chat_json([], schema=schema), {"approved": True})
        self.assertEqual(request.call_args.args[0]["format"], schema)
        self.assertEqual(request.call_args.args[0]["options"]["temperature"], 0)

    def test_json_array_rejected(self):
        gateway = OllamaGateway("fake")
        with patch.object(gateway, "_request", return_value={"message": {"content": "[]"}}):
            with self.assertRaises(RuntimeError):
                gateway.chat_json([])


if __name__ == "__main__":
    unittest.main()
