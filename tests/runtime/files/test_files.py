import io

import pytest
from botocore.exceptions import ClientError

from handler.runtime.files import LocalFileStore, S3FileStore


@pytest.fixture
def local(tmp_path):
    return LocalFileStore(tmp_path / "root")


class FakeS3:
    """Tiny in-memory stand-in for the boto3 S3 client."""

    class exceptions:
        NoSuchKey = type("NoSuchKey", (ClientError,), {})
        ClientError = ClientError

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self.page_size = 2

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = (Body, ContentType)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        more = start + self.page_size < len(keys)
        result = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            result["NextContinuationToken"] = str(start + self.page_size)
        return result


@pytest.fixture
def s3():
    client = FakeS3()
    return S3FileStore("bucket", "base", client), client


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalFileStore(tmp_path / "root")
    return S3FileStore("bucket", "base", FakeS3())


async def test_put_get_delete_list(store):
    await store.put("a/b.txt", b"one", "text/plain")
    await store.put("a/c/d.bin", b"\x00\x01")
    await store.put("ab/x", b"x")
    await store.put("top", b"t")
    assert await store.get("a/b.txt") == b"one"
    assert await store.get("missing") is None
    assert await store.list("a") == ["a/b.txt", "a/c/d.bin"]
    assert await store.list("a/") == ["a/b.txt", "a/c/d.bin"]
    assert await store.list("") == ["a/b.txt", "a/c/d.bin", "ab/x", "top"]
    assert await store.list("nothing") == []
    await store.put("a/b.txt", b"two")
    assert await store.get("a/b.txt") == b"two"
    assert await store.delete("a/b.txt")
    assert not await store.delete("a/b.txt")
    assert await store.get("a/b.txt") is None


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/abs", "a\\b", "", "..", "a/.."])
async def test_bad_keys_rejected(store, key):
    for call in (store.put(key, b"x"), store.get(key), store.delete(key)):
        with pytest.raises(ValueError):
            await call
    if key:
        with pytest.raises(ValueError):
            await store.list(key)


async def test_sync_round_trip(store, tmp_path):
    src = tmp_path / "src"
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "one.txt").write_text("1")
    (src / "sub" / "two.json").write_text("{}")
    (src / "sub" / "deep" / "three").write_bytes(b"\xff")
    assert await store.sync_up(src, "ws/1") == 3
    assert await store.list("ws/1") == ["ws/1/one.txt", "ws/1/sub/deep/three", "ws/1/sub/two.json"]
    await store.put("ws/10/other", b"no")
    dest = tmp_path / "dest"
    assert await store.sync_down("ws/1", dest) == 3
    assert (dest / "one.txt").read_text() == "1"
    assert (dest / "sub" / "two.json").read_text() == "{}"
    assert (dest / "sub" / "deep" / "three").read_bytes() == b"\xff"
    assert not (dest / "other").exists()
    assert await store.sync_down("empty", tmp_path / "e") == 0


async def test_local_symlink_escape_rejected(local, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    (tmp_path / "root" / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        await local.get("link/secret")
    with pytest.raises(ValueError):
        await local.put("link/new", b"x")


async def test_local_files_live_under_root(local, tmp_path):
    await local.put("x/y.txt", b"1")
    assert (tmp_path / "root" / "x" / "y.txt").read_bytes() == b"1"


async def test_s3_prefix_and_content_type(s3):
    store, client = s3
    await store.put("k/f.txt", b"hi", "text/plain")
    assert client.objects == {"base/k/f.txt": (b"hi", "text/plain")}
    assert await store.list("k") == ["k/f.txt"]
    await store.put("g", b"x")
    assert client.objects["base/g"][1] is None
    assert await store.list() == ["g", "k/f.txt"]


async def test_s3_paginates_list(s3):
    store, client = s3
    for i in range(5):
        await store.put(f"p/{i}", b"x")
    assert await store.list("p") == [f"p/{i}" for i in range(5)]


async def test_s3_from_env():
    env = {
        "BUCKETEER_BUCKET_NAME": "b", "BUCKETEER_AWS_ACCESS_KEY_ID": "id",
        "BUCKETEER_AWS_SECRET_ACCESS_KEY": "secret", "BUCKETEER_AWS_REGION": "us-east-1",
    }
    store = S3FileStore.from_env("pre", env)
    assert store._bucket == "b" and store._prefix == "pre"
    assert store._client.meta.region_name == "us-east-1"
