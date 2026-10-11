import gzip
import io
import os
import struct
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "_data")
BDE_DIR = os.path.join(DATA, "bde")

DISSECT_FVE_COMMIT = "0b3caf20c1e95136cb2eb4494303604e7629616b"
URL = ("https://raw.githubusercontent.com/fox-it/dissect.fve/%s/tests/_data/bde/%%s"
       % DISSECT_FVE_COMMIT)

RECOVERY = "284867-596541-514998-422114-660297-261613-215424-199408"
PASSPHRASE = "password12!@"

VOLUMES = {
    "recovery": ("recovery_password.bin.gz", lambda: "recovery:" + RECOVERY),
    "passphrase": ("aes-xts_128.bin.gz", lambda: "passphrase:" + PASSPHRASE),
    "bek": ("recovery_key.bin.gz",
            lambda: "bek:" + os.path.join(BDE_DIR, "recovery_key.bek")),
    "clear_key": ("suspended.bin.gz", lambda: None),
    "decrypted_state": ("decrypted.bin.gz", lambda: None),
}
EXTRA_FILES = ("recovery_key.bek",)


def fetch():
    os.makedirs(BDE_DIR, exist_ok=True)
    for name in [v[0] for v in VOLUMES.values()] + list(EXTRA_FILES):
        dst = os.path.join(BDE_DIR, name)
        if os.path.exists(dst):
            continue
        with urllib.request.urlopen(URL % name, timeout=60) as r:
            data = r.read()
        with open(dst + ".part", "wb") as fh:
            fh.write(data)
        os.replace(dst + ".part", dst)


def plain_ntfs():
    with gzip.open(os.path.join(DATA, "claude_ntfs.img.gz")) as g:
        return g.read()


def _unlock(bde, key_material):
    if bde.unlocked:
        return
    if key_material is None:
        bde.unlock_with_clear_key()
        return
    kind, _, value = key_material.partition(":")
    if kind == "recovery":
        bde.unlock_with_recovery_password(value)
    elif kind == "passphrase":
        bde.unlock_with_passphrase(value)
    elif kind == "bek":
        with open(value, "rb") as fh:
            bde.unlock_with_bek(fh)


def _xts_encrypt(key, data, sector, sector_size):
    from Crypto.Cipher import AES
    from Crypto.Util.strxor import strxor
    half = len(key) // 2
    aes, tw = AES.new(key[:half], AES.MODE_ECB), AES.new(key[half:], AES.MODE_ECB)
    n = len(data) // sector_size
    low = int.from_bytes(((1 << 127) - 1).to_bytes(16, "little") * n, "little")
    one = int.from_bytes((1).to_bytes(16, "little") * n, "little")
    ivs = b"".join((sector + i).to_bytes(8, "little") + bytes(8) for i in range(n))
    x = int.from_bytes(tw.encrypt(ivs), "little")
    tweaks = bytearray(len(data))
    for j in range(sector_size // 16):
        xb = x.to_bytes(16 * n, "little")
        for k in range(16):
            tweaks[j * 16 + k::sector_size] = xb[k::16]
        x = ((x & low) << 1) ^ (((x >> 127) & one) * 0x87)
    tweaks = bytes(tweaks)
    return strxor(aes.encrypt(strxor(bytes(data), tweaks)), tweaks)


def bitlocker_volume(method, plain=None):
    from dissect.fve.bde import BDE
    from dissect.fve.bde.bde import BitlockerStream, _consolidate_runs

    name, key_fn = VOLUMES[method]
    with gzip.open(os.path.join(BDE_DIR, name)) as g:
        container = g.read()
    key = key_fn()
    plain = plain_ntfs() if plain is None else plain

    bde = BDE(io.BytesIO(container))
    _unlock(bde, key)
    stream = bde.open()
    ss = stream.sector_size
    assert len(plain) <= stream.size and len(plain) % ss == 0
    if stream.encrypted:
        cipher = stream.cipher
        assert type(cipher).__name__ == "XtsMode", "AES-XTS 볼륨만 지원"
        fvek = cipher.key

    out = bytearray(container)
    pos = 0
    runs = _consolidate_runs(stream._iter_runs(0, len(plain)))
    for run_type, sector, count in runs:
        chunk = plain[pos:pos + count * ss]
        at = sector * ss
        if run_type == BitlockerStream.RUN_ENCRYPTED:
            out[at:at + len(chunk)] = _xts_encrypt(fvek, chunk, sector, ss)
        elif run_type == BitlockerStream.RUN_PLAIN:
            out[at:at + len(chunk)] = chunk
        elif run_type == BitlockerStream.RUN_SPARSE:
            assert not chunk.strip(b"\0"), "평문 데이터가 BitLocker 예약 영역에 걸린다"
        else:
            raise AssertionError("지원하지 않는 구간: %s" % run_type)
        pos += len(chunk)

    check = BDE(io.BytesIO(bytes(out)))
    _unlock(check, key)
    s2 = check.open()
    s2.seek(0)
    assert s2.read(len(plain)) == plain, "암호화 결과를 dissect 가 같은 평문으로 풀지 못함"
    return bytes(out), key


def mbr_disk(*volumes, gap=1024 * 1024):
    disk = bytearray(gap)
    lba = gap // 512
    body = bytearray()
    for i, vol in enumerate(volumes):
        vol = bytes(vol) + bytes(-len(vol) % (1024 * 1024))
        struct.pack_into("<BBBBBBBBII", disk, 446 + 16 * i, 0, 0, 0, 0, 0x07, 0xFE, 0xFF, 0xFF,
                         lba, len(vol) // 512)
        lba += len(vol) // 512
        body += vol
    disk[510:512] = b"\x55\xaa"
    return bytes(disk + body)
