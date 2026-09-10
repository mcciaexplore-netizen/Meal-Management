import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from PIL import Image, PngImagePlugin

from meal_management.errors import DependencyError, DomainError
from meal_management.storage import VercelBlobStorage


TOKEN = "vercel_blob_rw_fictional_" + "t" * 32
HOST = "fixturestore.private.blob.vercel-storage.com"
KEY = "a" * 64 + ".png"


def png_photo():
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Employee", "Fictional private metadata")
    Image.new("RGB", (10, 10), "blue").save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def blob_result(key=KEY, content_type="image/png", size=3, **overrides):
    values = {
        "pathname": key,
        "content_type": content_type,
        "url": "https://" + HOST + "/" + key,
        "size": size,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class VercelBlobStorageTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sdk.head.return_value = blob_result()
        self.requests = []
        self.responses = []
        self.stream = TrackedStream([b"abc"])
        self.response_headers = {"content-type": "image/png"}
        self.status = 200
        self.http = httpx.Client(transport=httpx.MockTransport(self.handle_request))
        self.addCleanup(self.http.close)
        self.storage = VercelBlobStorage(TOKEN, HOST, client=self.sdk, http_client=self.http)

    def handle_request(self, request):
        self.requests.append(request)
        response = httpx.Response(self.status, headers=self.response_headers, stream=self.stream)
        self.responses.append(response)
        return response

    def test_construction_is_inert_and_representation_hides_token(self):
        with patch.dict(sys.modules, {"vercel.blob": None}):
            storage = VercelBlobStorage(TOKEN, HOST)
        self.assertNotIn(TOKEN, repr(storage))
        self.sdk.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_missing_dependency_is_safe_and_lazy(self):
        with patch.dict(sys.modules, {"vercel.blob": None}):
            with self.assertRaisesRegex(DependencyError, "VERCEL_SDK_NOT_INSTALLED"):
                VercelBlobStorage(TOKEN, HOST).read(KEY)

    def test_constructor_rejects_missing_secret_and_public_host(self):
        for token, host in (("", HOST), (TOKEN, "fixturestore.public.blob.vercel-storage.com")):
            with self.subTest(host=host):
                with self.assertRaisesRegex(DomainError, "VERCEL_BLOB_CONFIGURATION_REQUIRED"):
                    VercelBlobStorage(token, host)

    def test_put_is_private_without_overwrite_and_strips_metadata(self):
        self.sdk.put.side_effect = lambda key, data, **options: blob_result(key, options["content_type"])
        photo = self.storage.put(png_photo(), "image/png")
        args, options = self.sdk.put.call_args
        self.assertRegex(photo.key, r"^[0-9a-f]{64}\.png$")
        self.assertEqual(args[0], photo.key)
        self.assertEqual(options, {
            "access": "private", "content_type": "image/png", "add_random_suffix": False,
            "overwrite": False,
        })
        self.assertEqual(photo.size, len(args[1]))
        with Image.open(io.BytesIO(args[1])) as image:
            self.assertNotIn("Employee", image.info)
        self.assertNotIn("url", vars(photo))
        self.sdk.close.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_put_uses_a_new_256_bit_key_each_time(self):
        self.sdk.put.side_effect = lambda key, data, **options: blob_result(key, options["content_type"])
        first = self.storage.put(png_photo(), "image/png")
        second = self.storage.put(png_photo(), "image/png")
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(first.key.split(".")[0]), 64)

    def test_invalid_upload_is_rejected_before_sdk_or_http(self):
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_TYPE"):
            self.storage.put(b"<svg></svg>", "image/svg+xml")
        self.sdk.put.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_put_refuses_public_provider_result(self):
        self.sdk.put.side_effect = lambda key, data, **options: blob_result(
            key, options["content_type"], url="https://fixturestore.public.blob.vercel-storage.com/" + key,
        )
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_STORAGE_RESPONSE"):
            self.storage.put(png_photo(), "image/png")

    def test_private_read_uses_exact_host_no_redirects_and_closes_stream(self):
        data, content_type = self.storage.read(KEY)
        self.assertEqual((data, content_type), (b"abc", "image/png"))
        self.sdk.head.assert_called_once_with(KEY)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://" + HOST + "/" + KEY)
        self.assertEqual(request.headers["authorization"], "Bearer " + TOKEN)
        self.assertEqual(request.headers["accept-encoding"], "identity")
        self.assertEqual(request.extensions["timeout"]["read"], 20.0)
        self.assertTrue(self.stream.closed)
        self.assertTrue(self.responses[0].is_closed)
        self.assertTrue(self.http.is_closed)
        self.sdk.close.assert_called_once()

    def test_read_does_not_follow_redirects_or_forward_secret(self):
        self.status = 302
        self.response_headers["location"] = "https://attacker.example.test/private"
        with self.assertRaisesRegex(DomainError, "PHOTO_STORAGE_UNAVAILABLE"):
            self.storage.read(KEY)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.stream.closed)

    def test_read_404_is_safe(self):
        self.status = 404
        with self.assertRaisesRegex(DomainError, "PHOTO_NOT_FOUND"):
            self.storage.read(KEY)
        self.assertTrue(self.stream.closed)

    def test_read_rejects_mismatched_or_unsafe_provider_url_before_http(self):
        for url in (
            "http://" + HOST + "/" + KEY,
            "https://otherstore.private.blob.vercel-storage.com/" + KEY,
            "https://" + HOST + ":443/" + KEY,
            "https://user:password@" + HOST + "/" + KEY,
            "https://" + HOST + "/" + KEY + "?token=" + TOKEN,
            "https://" + HOST + "/" + KEY + "#",
            "https://" + HOST + "/" + "b" * 64 + ".png",
        ):
            with self.subTest(url=url):
                self.sdk.head.return_value = blob_result(url=url)
                with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_STORAGE_RESPONSE"):
                    self.storage.read(KEY)
        self.assertEqual(self.requests, [])

    def test_read_rejects_invalid_metadata_sizes_before_http(self):
        for size in (None, 0, -1, True, "3", self.storage.max_bytes + 1):
            with self.subTest(size=size):
                self.sdk.head.return_value = blob_result(size=size)
                with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_SIZE"):
                    self.storage.read(KEY)
        self.assertEqual(self.requests, [])

    def test_read_enforces_bound_when_metadata_and_content_length_are_wrong(self):
        self.storage.max_bytes = 100
        self.sdk.head.return_value = blob_result(size=50)
        self.stream = TrackedStream([b"a" * 101, b"unread"])
        self.response_headers["content-length"] = "50"
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_SIZE"):
            self.storage.read(KEY)
        self.assertEqual(self.stream.consumed, 1)
        self.assertTrue(self.stream.closed)
        self.assertTrue(self.http.is_closed)

    def test_read_rejects_oversized_content_length_without_consuming_body(self):
        self.response_headers["content-length"] = str(self.storage.max_bytes + 1)
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_SIZE"):
            self.storage.read(KEY)
        self.assertEqual(self.stream.consumed, 0)
        self.assertTrue(self.stream.closed)

    def test_read_rejects_compressed_or_mismatched_response_content(self):
        self.response_headers["content-encoding"] = "gzip"
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_STORAGE_RESPONSE"):
            self.storage.read(KEY)
        self.assertEqual(self.stream.consumed, 0)
        self.assertTrue(self.stream.closed)

    def test_read_rejects_truncated_object_and_closes(self):
        self.sdk.head.return_value = blob_result(size=4)
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_SIZE"):
            self.storage.read(KEY)
        self.assertTrue(self.stream.closed)

    def test_read_network_error_is_secret_safe_and_closes(self):
        self.stream = TrackedStream([httpx.ReadError("provider private detail " + TOKEN)])
        with self.assertRaises(DomainError) as caught:
            self.storage.read(KEY)
        self.assertEqual(str(caught.exception), "PHOTO_STORAGE_UNAVAILABLE")
        self.assertNotIn(TOKEN, repr(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertTrue(self.stream.closed)
        self.assertTrue(self.http.is_closed)

    def test_delete_uses_only_validated_key_and_closes_client(self):
        self.storage.delete(KEY)
        self.sdk.head.assert_called_once_with(KEY)
        self.sdk.delete.assert_called_once_with(KEY)
        self.assertEqual(self.sdk.close.call_count, 2)
        self.assertEqual(self.requests, [])

    def test_delete_refuses_different_store_and_never_deletes(self):
        self.sdk.head.return_value = blob_result(
            url="https://otherstore.private.blob.vercel-storage.com/" + KEY,
        )
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_STORAGE_RESPONSE"):
            self.storage.delete(KEY)
        self.sdk.delete.assert_not_called()

    def test_delete_missing_object_is_idempotent(self):
        with patch.object(self.storage, "_sdk_call", side_effect=DomainError("PHOTO_NOT_FOUND")) as call:
            self.storage.delete(KEY)
        call.assert_called_once_with("head", KEY)

    def test_sdk_errors_never_expose_details(self):
        for operation in ("head", "delete", "put"):
            with self.subTest(operation=operation):
                client = Mock()
                client.head.return_value = blob_result()
                getattr(client, operation).side_effect = RuntimeError("provider detail " + TOKEN)
                storage = VercelBlobStorage(TOKEN, HOST, client=client)
                with self.assertRaises(DomainError) as caught:
                    if operation == "put":
                        storage.put(png_photo(), "image/png")
                    else:
                        getattr(storage, "read" if operation == "head" else "delete")(KEY)
                self.assertEqual(str(caught.exception), "PHOTO_STORAGE_UNAVAILABLE")
                self.assertNotIn(TOKEN, repr(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(client.close.call_count, 2 if operation == "delete" else 1)

    def test_invalid_read_and_delete_keys_do_not_reach_provider(self):
        for key in ("../secret", "https://" + HOST + "/" + KEY, "a" * 64 + ".svg"):
            with self.subTest(key=key):
                for operation in (self.storage.read, self.storage.delete):
                    with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_KEY"):
                        operation(key)
        self.sdk.head.assert_not_called()
        self.sdk.delete.assert_not_called()
        self.assertEqual(self.requests, [])
