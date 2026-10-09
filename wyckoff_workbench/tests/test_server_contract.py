import unittest
from email.message import Message

from wyckoff_cn.server import _is_json_content_type


class ServerHeaderContractTests(unittest.TestCase):
    def test_accepts_json_with_charset_parameter(self):
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"
        self.assertTrue(_is_json_content_type(headers))

    def test_rejects_non_json_or_missing_content_type(self):
        for value in (None, "text/plain", "application/x-www-form-urlencoded"):
            with self.subTest(value=value):
                headers = Message()
                if value is not None:
                    headers["Content-Type"] = value
                self.assertFalse(_is_json_content_type(headers))


if __name__ == "__main__":
    unittest.main()
