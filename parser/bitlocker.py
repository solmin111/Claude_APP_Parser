import io

import pytsk3

from .errors import VolumeLockedError

SIGNATURE = b"-FVE-FS-"
KEY_KINDS = ("recovery", "passphrase", "bek", "fvek")
KEY_ENV = "ERELM_KEY_MATERIAL"


def is_bitlocker(img, offset):
    try:
        return img.read(offset, 512)[3:11] == SIGNATURE
    except (OSError, RuntimeError):
        return False


def key_kind(key_material):
    kind, _, value = (key_material or "").partition(":")
    return kind if value and kind in KEY_KINDS else None


class _PartitionFile:

    def __init__(self, img, offset, size):
        self._img, self._offset, self.size, self._pos = img, offset, size, 0

    def seek(self, pos, whence=io.SEEK_SET):
        self._pos = max(0, pos + (0, self._pos, self.size)[whence])
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        left = self.size - self._pos
        n = left if n is None or n < 0 else min(n, left)
        if n <= 0:
            return b""
        data = self._img.read(self._offset + self._pos, n)
        self._pos += len(data)
        return data


class _PlainImgInfo(pytsk3.Img_Info):

    def __init__(self, stream):
        self._s = stream
        super().__init__(url="", type=pytsk3.TSK_IMG_TYPE_EXTERNAL)

    def close(self):
        pass

    def read(self, offset, size):
        self._s.seek(offset)
        return self._s.read(size)

    def get_size(self):
        return self._s.size


class _FastXts:

    def __init__(self, slow):
        from Crypto.Cipher import AES
        from Crypto.Util.strxor import strxor
        self._slow, self._size, self._xor = slow, slow.sector_size, strxor
        half = slow.block_size
        self._aes = AES.new(slow.key[:half], AES.MODE_ECB)
        self._tweak = AES.new(slow.key[half:], AES.MODE_ECB)
        self._n = 0

    def decrypt(self, data, sector=0, output=None):
        size = self._size
        n = len(data) // size
        if output is not None or n == 0 or len(data) % size or size % 16:
            return self._slow.decrypt(data, sector, output)
        if n != self._n:
            self._n = n
            self._low = int.from_bytes(((1 << 127) - 1).to_bytes(16, "little") * n, "little")
            self._one = int.from_bytes((1).to_bytes(16, "little") * n, "little")
        ivs = b"".join((sector + i).to_bytes(8, "little") + bytes(8) for i in range(n))
        x = int.from_bytes(self._tweak.encrypt(ivs), "little")
        tweaks = bytearray(len(data))
        for j in range(size // 16):
            xb = x.to_bytes(16 * n, "little")
            for k in range(16):
                tweaks[j * 16 + k::size] = xb[k::16]
            x = ((x & self._low) << 1) ^ (((x >> 127) & self._one) * 0x87)
        tweaks = bytes(tweaks)
        return self._xor(self._aes.decrypt(self._xor(bytes(data), tweaks)), tweaks)


def _speed_up(stream):
    try:
        from dissect.fve.crypto._pycryptodome import XtsMode
        from dissect.fve.crypto.base import Plain64
        c = stream.cipher
        if (isinstance(c, XtsMode) and isinstance(c.iv_mode, Plain64)
                and c.iv_sector_size == c.sector_size):
            stream.cipher = _FastXts(c)
    except Exception:  # noqa: BLE001
        pass


def open_volume(img, offset, size, key_material=None):
    try:
        from dissect.fve.bde import BDE
    except ImportError as exc:
        raise VolumeLockedError("BitLocker 볼륨을 풀려면 dissect.fve 가 필요합니다 "
                                "(pip install -r requirements.txt)", missing=str(exc))
    try:
        bde = BDE(_PartitionFile(img, offset, size))
        protectors = {"clear_key": bool(bde.has_clear_key()),
                      "recovery_password": bool(bde.has_recovery_password()),
                      "passphrase": bool(bde.has_passphrase()),
                      "bek": bool(bde.has_bek())}
    except Exception as exc:  # noqa: BLE001
        raise VolumeLockedError("BitLocker 메타데이터를 읽을 수 없습니다: %s" % _short(exc))

    method, clear_error = None, None
    if bde.unlocked:
        method = "decrypted_state"
    elif protectors["clear_key"]:
        try:
            bde.unlock_with_clear_key()
            method = "clear_key"
        except Exception as exc:  # noqa: BLE001
            clear_error = _short(exc)

    if method is None:
        kind, _, value = (key_material or "").partition(":")
        if not value:
            raise VolumeLockedError(
                "BitLocker 볼륨이 잠겨 있습니다. 키가 필요합니다 (--key-material 또는 "
                "환경변수 %s)" % KEY_ENV,
                protectors=protectors, clear_key_error=clear_error)
        if kind not in KEY_KINDS:
            raise VolumeLockedError(
                "--key-material 은 recovery: / passphrase: / bek: / fvek: 로 시작해야 합니다",
                protectors=protectors)
        try:
            if kind == "recovery":
                bde.unlock_with_recovery_password(value)
            elif kind == "passphrase":
                bde.unlock_with_passphrase(value)
            elif kind == "bek":
                with open(value, "rb") as fh:
                    bde.unlock_with_bek(fh)
            else:
                bde.unlock_with_fvek(bytes.fromhex(value))
        except Exception as exc:  # noqa: BLE001
            raise VolumeLockedError(
                "받은 키(%s)로 BitLocker 볼륨을 풀지 못했습니다: %s"
                % (kind, _short(exc, secret=value)),
                protectors=protectors, key_kind=kind)
        method = kind

    stream = bde.open()
    _speed_up(stream)
    return _PlainImgInfo(stream), method, protectors


def _short(exc, secret=None):
    s = ("%s: %s" % (type(exc).__name__, exc)).splitlines()[0][:200]
    if secret:
        s = s.replace(secret, "<key>")
    return s


def demo():
    import os

    from dissect.fve.crypto import create_cipher
    for bits, key_len in ((128, 32), (256, 64)):
        slow = create_cipher("aes-xts-%d-plain64" % bits, os.urandom(key_len),
                             sector_size=512, iv_sector_size=512)
        fast = _FastXts(slow)
        for sectors, first in ((1, 0), (128, 12345), (300, 2 ** 33 + 5)):
            buf = os.urandom(512 * sectors)
            assert fast.decrypt(buf, first) == slow.decrypt(buf, first), (bits, sectors)

    class FakeImg:
        data = bytes(range(256)) * 8

        def read(self, offset, size):
            return self.data[offset:offset + size]
    f = _PartitionFile(FakeImg(), 512, 1024)
    assert f.read(4) == FakeImg.data[512:516] and f.tell() == 4
    assert f.seek(-8, io.SEEK_END) == 1016 and f.read() == FakeImg.data[1528:1536]
    assert f.read(1) == b""
    assert is_bitlocker(FakeImg(), 0) is False
    assert key_kind("recovery:1-2") == "recovery" and key_kind("fvek:") is None
    assert key_kind("password:x") is None
    print("bitlocker demo ok")


if __name__ == "__main__":
    demo()
