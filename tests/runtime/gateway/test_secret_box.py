import pytest

from handler.runtime.gateway.secret_box import EncryptedSecrets, SecretBox, SecretUnreadable
from handler.runtime.store.sqlite import SQLiteStore


def test_encrypt_round_trip_and_wrong_key():
    box = SecretBox("key one")
    token = box.encrypt("refresh-token")
    assert "refresh-token" not in token and box.decrypt(token) == "refresh-token"
    with pytest.raises(SecretUnreadable):
        SecretBox("key two").decrypt(token)
    with pytest.raises(ValueError):
        SecretBox("")


def test_signed_payloads_expire_and_reject_tampering():
    box = SecretBox("k")
    signed = box.sign({"n": "abc"}, 60, now=1000)
    assert box.verify(signed, now=1059) == {"n": "abc", "exp": 1060}
    assert box.verify(signed, now=1061) is None
    data, _, mac = signed.partition(".")
    assert box.verify(data[:-1] + "A." + mac, now=1000) is None
    assert box.verify(signed, now=1000) and SecretBox("other").verify(signed, now=1000) is None
    assert box.verify("", now=1000) is None and box.verify("garbage", now=1000) is None


async def test_encrypted_secrets_store_only_ciphertext(tmp_path):
    store = SQLiteStore(tmp_path / "s.sqlite")
    try:
        secrets = EncryptedSecrets(store, SecretBox("k"))
        await secrets.put("a", "plain")
        assert await store.get_secret("a") != "plain"
        assert await secrets.get("a") == "plain"
        assert await secrets.delete("a") and await secrets.get("a") is None
    finally:
        await store.close()
