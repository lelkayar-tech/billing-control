import unittest

from app_mode import (
    is_mutating_request,
    is_request_allowed,
    resolve_app_mode,
)


class ResolveAppModeTests(unittest.TestCase):
    def test_edit_requires_explicit_value(self) -> None:
        self.assertEqual(resolve_app_mode("edit"), "edit")
        self.assertEqual(resolve_app_mode(" EDIT "), "edit")

    def test_missing_or_unknown_value_falls_back_to_read(self) -> None:
        for value in (None, "", "read", "readonly", "edti", "anything"):
            with self.subTest(value=value):
                self.assertEqual(resolve_app_mode(value), "read")


class MutatingRequestTests(unittest.TestCase):
    def test_safe_methods_are_not_mutating(self) -> None:
        for method in ("GET", "HEAD", "OPTIONS", "get"):
            with self.subTest(method=method):
                self.assertFalse(is_mutating_request(method))

    def test_write_methods_are_mutating(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            with self.subTest(method=method):
                self.assertTrue(is_mutating_request(method))


class RequestPermissionTests(unittest.TestCase):
    def test_read_mode_blocks_writes_but_allows_reads(self) -> None:
        self.assertTrue(is_request_allowed("read", "GET"))
        self.assertFalse(is_request_allowed("read", "POST"))
        self.assertFalse(is_request_allowed("", "DELETE"))

    def test_edit_mode_allows_writes(self) -> None:
        self.assertTrue(is_request_allowed("edit", "POST"))
        self.assertTrue(is_request_allowed("edit", "DELETE"))


if __name__ == "__main__":
    unittest.main()
