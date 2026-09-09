import base64
import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from meal_management.errors import DomainError
from meal_management.models import ScanInput, ServerContext
from meal_management.security import (
    generate_token,
    hash_password,
    normalize_email,
    payload_digest,
    required_text,
    token_digest,
    utc_naive,
    verify_password,
)


class TokenTests(unittest.TestCase):
    def test_random_tokens_are_distinct_and_256_bits(self):
        tokens = {generate_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)
        for token in tokens:
            self.assertEqual(len(token), 43)
            raw = base64.urlsafe_b64decode(token + "=")
            self.assertEqual(len(raw), 32)
            self.assertEqual(token_digest(token), hashlib.sha256(raw).digest())

    def test_invalid_and_noncanonical_tokens_are_rejected(self):
        invalid = (None, "", "employee-001", "A" * 42, "A" * 44, "A" * 42 + "B", "!" * 43)
        for token in invalid:
            with self.subTest(token=token):
                with self.assertRaises(DomainError):
                    token_digest(token)

    def test_secret_fields_are_not_in_model_representations(self):
        token = generate_token()
        context = ServerContext(token)
        scan = ScanInput("97f33cb1-5449-4689-847d-48b8a8a86621", token, 1, "SCANNER")
        self.assertNotIn(token, repr(context))
        self.assertNotIn(token, repr(scan))

    def test_payload_digest_is_independent_of_mapping_order(self):
        first = {"qr": "hashed-token", "quantity": 2, "visitor": {"name": "A", "purpose": "B"}}
        second = {"visitor": {"purpose": "B", "name": "A"}, "quantity": 2, "qr": "hashed-token"}
        self.assertEqual(payload_digest(first), payload_digest(second))

    def test_payload_digest_changes_when_serving_inputs_change(self):
        self.assertNotEqual(payload_digest({"quantity": 1}), payload_digest({"quantity": 2}))

    def test_payload_digest_rejects_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            payload_digest({"quantity": float("nan")})


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "A fictional password for unit tests"
        cls.encoded = hash_password(cls.password)

    def test_password_hash_verifies_only_the_correct_password(self):
        self.assertTrue(verify_password(self.password, self.encoded))
        self.assertFalse(verify_password("Incorrect fictional password", self.encoded))
        self.assertNotIn(self.password, self.encoded)

    def test_passwords_receive_independent_random_salts(self):
        self.assertNotEqual(hash_password(self.password), self.encoded)

    def test_password_length_limits_are_enforced(self):
        for password in (None, "short", "a" * 1025):
            with self.subTest(length=None if password is None else len(password)):
                with self.assertRaises(DomainError):
                    hash_password(password)

    def test_malformed_password_hashes_fail_closed(self):
        for encoded in (None, "", "plaintext", "scrypt$999999999$8$1$a$b", "argon2$32768$8$1$a$b"):
            with self.subTest(encoded=encoded):
                self.assertFalse(verify_password(self.password, encoded))


class ValidationTests(unittest.TestCase):
    def test_email_is_trimmed_and_normalized(self):
        self.assertEqual(normalize_email(" Asha.Rao@EXAMPLE.COM "), "asha.rao@example.com")

    def test_invalid_email_is_rejected(self):
        for value in (None, "", "missing-at", "a@@example.com", "a\n@example.com", "a@example"):
            with self.subTest(value=value):
                with self.assertRaises(DomainError):
                    normalize_email(value)

    def test_required_text_rejects_blank_oversized_and_control_characters(self):
        for value in (None, "  ", "x" * 11, "name\n"):
            with self.subTest(value=value):
                with self.assertRaises(DomainError):
                    required_text(value, "name", 10)

    def test_aware_datetime_is_converted_to_utc(self):
        local = datetime(2026, 9, 9, 12, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(utc_naive(local), datetime(2026, 9, 9, 7, 0))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(DomainError):
            utc_naive(datetime(2026, 9, 9, 12, 30))


if __name__ == "__main__":
    unittest.main()
