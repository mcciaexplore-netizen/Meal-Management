import io
import os
import re
import secrets
import stat
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import DependencyError, DomainError


@dataclass(frozen=True)
class StoredPhoto:
    key: str
    content_type: str
    size: int


def _photo_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png)", key):
        raise DomainError("INVALID_PHOTO_KEY")
    return key


def _content_type(key):
    return "image/jpeg" if key.endswith(".jpg") else "image/png"


def sanitize_photo(data, content_type, max_bytes):
    if not isinstance(data, bytes) or not data or len(data) > max_bytes:
        raise DomainError("INVALID_PHOTO_SIZE")
    if content_type not in {"image/jpeg", "image/png"}:
        raise DomainError("INVALID_PHOTO_TYPE")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise DependencyError("PILLOW_NOT_INSTALLED") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as uploaded:
                if (
                    uploaded.format not in {"JPEG", "PNG"}
                    or uploaded.width * uploaded.height > 8_000_000
                    or uploaded.width < 1
                    or uploaded.height < 1
                    or getattr(uploaded, "n_frames", 1) != 1
                ):
                    raise DomainError("INVALID_PHOTO")
                actual_type = "image/jpeg" if uploaded.format == "JPEG" else "image/png"
                if content_type != actual_type:
                    raise DomainError("PHOTO_CONTENT_TYPE_MISMATCH")
                image_format = uploaded.format
                uploaded.verify()
            with Image.open(io.BytesIO(data)) as uploaded:
                uploaded.load()
                mode = "RGBA" if image_format == "PNG" and "A" in uploaded.getbands() else "RGB"
                converted = uploaded.convert(mode)
                clean = Image.new(mode, converted.size)
                clean.paste(converted)
                output = io.BytesIO()
                if image_format == "JPEG":
                    clean.save(output, format="JPEG", quality=90)
                else:
                    clean.save(output, format="PNG")
                result = output.getvalue()
    except DomainError:
        raise
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise DomainError("INVALID_PHOTO") from None
    if len(result) > max_bytes:
        raise DomainError("INVALID_PHOTO_SIZE")
    return result, actual_type


class LocalFileStorage:
    def __init__(self, root, max_bytes=5 * 1024 * 1024):
        self.root = Path(root).absolute()
        self.max_bytes = max_bytes

    @contextmanager
    def _directory(self, create=False):
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise DomainError("UNSAFE_PHOTO_STORAGE")
        if create:
            missing = []
            current = self.root
            while not current.exists():
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, exist_ok=True)
        if not self.root.exists():
            raise DomainError("PHOTO_NOT_FOUND")
        resolved = self.root.resolve(strict=True)
        if resolved != self.root:
            raise DomainError("UNSAFE_PHOTO_STORAGE")
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise DomainError("UNSAFE_PHOTO_STORAGE") from None
        try:
            os.fchmod(descriptor, 0o700)
            yield descriptor
        finally:
            os.close(descriptor)

    def put(self, data, content_type):
        normalized, actual_type = sanitize_photo(data, content_type, self.max_bytes)
        extension = ".jpg" if actual_type == "image/jpeg" else ".png"
        with self._directory(create=True) as directory:
            for attempt in range(3):
                key = secrets.token_hex(32) + extension
                try:
                    descriptor = os.open(
                        key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=directory,
                    )
                    break
                except FileExistsError:
                    if attempt == 2:
                        raise DomainError("PHOTO_STORAGE_UNAVAILABLE") from None
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(normalized)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                os.unlink(key, dir_fd=directory)
                raise
        return StoredPhoto(key=key, content_type=actual_type, size=len(normalized))

    def read(self, key):
        key = _photo_key(key)
        with self._directory() as directory:
            try:
                descriptor = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                raise DomainError("PHOTO_NOT_FOUND") from None
            except OSError:
                raise DomainError("UNSAFE_PHOTO_STORAGE") from None
            with os.fdopen(descriptor, "rb") as source:
                attributes = os.fstat(source.fileno())
                if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
                    raise DomainError("UNSAFE_PHOTO_STORAGE")
                data = source.read(self.max_bytes + 1)
        if not data or len(data) > self.max_bytes:
            raise DomainError("INVALID_PHOTO_SIZE")
        return data, _content_type(key)

    def delete(self, key):
        key = _photo_key(key)
        with self._directory() as directory:
            try:
                attributes = os.stat(key, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
                    raise DomainError("UNSAFE_PHOTO_STORAGE")
                os.unlink(key, dir_fd=directory)
            except FileNotFoundError:
                return


class S3Storage:
    def __init__(self, bucket, region, max_bytes=5 * 1024 * 1024, client=None):
        if not bucket or not region:
            raise DomainError("S3_CONFIGURATION_REQUIRED")
        self.bucket = bucket
        self.region = region
        self.max_bytes = max_bytes
        self._client = client

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise DependencyError("BOTO3_NOT_INSTALLED") from None
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def put(self, data, content_type):
        normalized, actual_type = sanitize_photo(data, content_type, self.max_bytes)
        key = secrets.token_hex(32) + (".jpg" if actual_type == "image/jpeg" else ".png")
        self._get_client().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=normalized,
            ContentType=actual_type,
            ServerSideEncryption="AES256",
        )
        return StoredPhoto(key=key, content_type=actual_type, size=len(normalized))

    def read(self, key):
        key = _photo_key(key)
        response = self._get_client().get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            data = stream.read(self.max_bytes + 1)
        finally:
            stream.close()
        if not data or len(data) > self.max_bytes:
            raise DomainError("INVALID_PHOTO_SIZE")
        return data, _content_type(key)

    def delete(self, key):
        self._get_client().delete_object(Bucket=self.bucket, Key=_photo_key(key))


class VercelBlobStorage:
    def __init__(self, token, store_host, max_bytes=5 * 1024 * 1024, client=None, http_client=None):
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{32,512}", token):
            raise DomainError("VERCEL_BLOB_CONFIGURATION_REQUIRED")
        if not isinstance(store_host, str) or not re.fullmatch(
            r"[a-z0-9]+\.private\.blob\.vercel-storage\.com", store_host,
        ):
            raise DomainError("VERCEL_BLOB_CONFIGURATION_REQUIRED")
        self._token = token
        self.store_host = store_host
        self.max_bytes = max_bytes
        self._client = client
        self._http_client = http_client

    def _url(self, key):
        return "https://" + self.store_host + "/" + _photo_key(key)

    def _validate_result(self, result, key, content_type):
        try:
            parsed = urlsplit(result.url)
            valid = (
                parsed.scheme == "https"
                and parsed.netloc == self.store_host
                and parsed.path == "/" + key
                and not parsed.query
                and not parsed.fragment
                and result.url == self._url(key)
                and result.pathname == key
                and result.content_type == content_type
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise DomainError("INVALID_PHOTO_STORAGE_RESPONSE")

    def _sdk_call(self, method, *args, **kwargs):
        client = self._client
        not_found = ()
        if client is None:
            try:
                from vercel.blob import BlobClient, BlobNotFoundError
            except ImportError:
                raise DependencyError("VERCEL_SDK_NOT_INSTALLED") from None
            not_found = (BlobNotFoundError,)
            try:
                client = BlobClient(token=self._token)
            except Exception:
                raise DomainError("PHOTO_STORAGE_UNAVAILABLE") from None
        try:
            try:
                return getattr(client, method)(*args, **kwargs)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        except not_found:
            raise DomainError("PHOTO_NOT_FOUND") from None
        except Exception:
            raise DomainError("PHOTO_STORAGE_UNAVAILABLE") from None

    @contextmanager
    def _http(self):
        client = self._http_client
        if client is None:
            try:
                import httpx
            except ImportError:
                raise DependencyError("HTTPX_NOT_INSTALLED") from None
            client = httpx.Client(timeout=20.0, follow_redirects=False, trust_env=False)
        try:
            yield client
        finally:
            client.close()

    def put(self, data, content_type):
        normalized, actual_type = sanitize_photo(data, content_type, self.max_bytes)
        key = secrets.token_hex(32) + (".jpg" if actual_type == "image/jpeg" else ".png")
        result = self._sdk_call(
            "put", key, normalized, access="private", content_type=actual_type,
            add_random_suffix=False, overwrite=False,
        )
        self._validate_result(result, key, actual_type)
        return StoredPhoto(key=key, content_type=actual_type, size=len(normalized))

    def read(self, key):
        key = _photo_key(key)
        content_type = _content_type(key)
        metadata = self._sdk_call("head", key)
        self._validate_result(metadata, key, content_type)
        expected_size = getattr(metadata, "size", None)
        if type(expected_size) is not int or not 0 < expected_size <= self.max_bytes:
            raise DomainError("INVALID_PHOTO_SIZE")
        try:
            with self._http() as client:
                with client.stream(
                    "GET", self._url(key),
                    headers={"Authorization": "Bearer " + self._token, "Accept-Encoding": "identity"},
                    timeout=20.0, follow_redirects=False,
                ) as response:
                    if response.status_code == 404:
                        raise DomainError("PHOTO_NOT_FOUND")
                    if response.status_code != 200:
                        raise DomainError("PHOTO_STORAGE_UNAVAILABLE")
                    if (
                        response.headers.get("content-type", "").split(";", 1)[0].strip() != content_type
                        or response.headers.get("content-encoding", "identity").lower() != "identity"
                    ):
                        raise DomainError("INVALID_PHOTO_STORAGE_RESPONSE")
                    declared_size = response.headers.get("content-length")
                    if declared_size is not None and (
                        not declared_size.isascii() or not declared_size.isdecimal()
                        or not 0 < int(declared_size) <= self.max_bytes
                    ):
                        raise DomainError("INVALID_PHOTO_SIZE")
                    data = bytearray()
                    for chunk in response.iter_raw(chunk_size=min(65536, self.max_bytes + 1)):
                        if len(data) + len(chunk) > self.max_bytes:
                            raise DomainError("INVALID_PHOTO_SIZE")
                        data.extend(chunk)
                    if not data or len(data) != expected_size:
                        raise DomainError("INVALID_PHOTO_SIZE")
        except DomainError:
            raise
        except Exception:
            raise DomainError("PHOTO_STORAGE_UNAVAILABLE") from None
        return bytes(data), content_type

    def delete(self, key):
        key = _photo_key(key)
        try:
            metadata = self._sdk_call("head", key)
        except DomainError as error:
            if error.code == "PHOTO_NOT_FOUND":
                return
            raise
        self._validate_result(metadata, key, _content_type(key))
        self._sdk_call("delete", key)


def storage_from_settings(settings):
    if settings.photo_backend == "local":
        if settings.environment == "production":
            raise DomainError("LOCAL_STORAGE_FORBIDDEN_IN_PRODUCTION")
        return LocalFileStorage(settings.photo_root, settings.max_photo_bytes)
    if settings.photo_backend == "s3":
        return S3Storage(settings.photo_bucket, settings.aws_region, settings.max_photo_bytes)
    if settings.photo_backend == "vercel_blob":
        return VercelBlobStorage(
            settings.blob_read_write_token, settings.blob_store_host, settings.max_photo_bytes,
        )
    raise DomainError("INVALID_PHOTO_BACKEND")
