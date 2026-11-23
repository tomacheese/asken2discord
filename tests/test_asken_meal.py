import unittest
from unittest import mock

from asken_client import AskenClient


class FetchMealTest(unittest.TestCase):
    def test_空の食事データを正しく取得する(self):
        with AskenClient("user", "password") as client:
            response = mock.Mock(text="V2WspMeal.eatDatas = {};")
            with mock.patch.object(
                client.session, "get", return_value=response
            ):
                meal = client.fetch_meal("lunch", "2026-11-15")
        self.assertFalse(meal.has_content)

    def test_予期しない応答を空の食事として扱わない(self):
        with AskenClient("user", "password") as client:
            for body in (
                "<html>login</html>",
                "V2WspMeal.eatDatas = {invalid};",
            ):
                with self.subTest(body=body):
                    response = mock.Mock(text=body)
                    with mock.patch.object(
                        client.session, "get", return_value=response
                    ):
                        with self.assertRaises(ValueError):
                            client.fetch_meal("lunch", "2026-11-15")
