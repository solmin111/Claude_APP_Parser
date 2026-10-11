import hashlib
import os
import re

import pyewf
import pytsk3

from .errors import ImageOpenError, MissingSegmentError, UnsupportedFormatError

EWF1_SIG = b"EVF\x09\x0d\x0a\xff\x00"
UNSUPPORTED_SIGS = (
    (b"EVF2\x0d\x0a\x81\x00", "EWF2(.Ex01)"),
    (b"LVF\x09\x0d\x0a\xff\x00", "L01"),
    (b"KDMV", "VMDK"),
    (b"QFI\xfb", "QCOW"),
    (b"conectix", "VHD"),
)
_SPLIT_RAW_RE = re.compile(r"^(?P<stem>.+)\.(?P<num>\d{3})$")
_EWF_SEG_RE = re.compile(r"\.[Ee]([0-9]{2}|[A-Za-z]{2})$")


class _EwfImgInfo(pytsk3.Img_Info):

    def __init__(self, handle):
        self._h = handle
        super().__init__(url="", type=pytsk3.TSK_IMG_TYPE_EXTERNAL)

    def close(self):
        self._h.close()

    def read(self, offset, size):
        self._h.seek(offset)
        return self._h.read(size)

    def get_size(self):
        return self._h.get_media_size()


class Image:
    def __init__(self, path, img, image_type, segments, warnings=()):
        self.path = path
        self.img = img
        self.image_type = image_type
        self.segments = segments
        self.warnings = list(warnings)

    def close(self):
        self.img.close()

    def describe(self, hash_limit=64 * 1024 * 1024):
        sha, n = _sha256_prefix(self.segments[0], hash_limit)
        name = os.path.basename(self.path)
        return {
            "image_id": "%s (sha256[prefix]=%s)" % (name, sha[:16]),
            "image_type": self.image_type,
            "file_name": name,
            "path": self.path,
            "segments": [os.path.basename(s) for s in self.segments],
            "size_bytes": self.img.get_size(),
            "reader": ("libewf %s" % pyewf.get_version() if self.image_type == "ewf"
                       else "The Sleuth Kit %s" % pytsk3.TSK_VERSION_STR),
            "sha256_first_segment_prefix": {
                "sha256": sha, "hashed_bytes": n,
                "note": "첫 세그먼트 선두 %d 바이트 한정 해시(대용량 이미지 식별용)" % hash_limit},
            "warnings": self.warnings,
        }


def open_image(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ImageOpenError("이미지 파일이 존재하지 않습니다", path=path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ImageOpenError("이미지를 열 수 없습니다: %s" % exc, path=path)
    for sig, name in UNSUPPORTED_SIGS:
        if head.startswith(sig):
            raise UnsupportedFormatError("지원하지 않는 이미지 형식입니다: %s" % name,
                                         path=path)
    if head == EWF1_SIG:
        return _open_ewf(path)
    return _open_raw(path)


def _open_ewf(path):
    try:
        segments = pyewf.glob(path)
    except OSError as exc:
        raise ImageOpenError("E01 세그먼트 탐색 실패: %s" % _short(exc), path=path)
    _check_ewf_sequence(path, segments)
    names = [os.path.basename(s) for s in segments]
    if _last_ewf_section(segments[-1]) == b"next":
        raise MissingSegmentError("E01 마지막 세그먼트 누락 ('%s' 가 next 섹션으로 끝남)"
                                  % names[-1], path=path, segments=names)
    handle = pyewf.handle()
    try:
        handle.open(segments)
    except OSError as exc:
        raise ImageOpenError("E01 을 열 수 없습니다: %s" % _short(exc), path=path,
                             segments=names)
    img = _EwfImgInfo(handle)
    size = img.get_size()
    for off in (0, max(0, size - 512)):
        try:
            img.read(off, 512)
        except (OSError, RuntimeError) as exc:
            img.close()
            raise ImageOpenError("E01 데이터 읽기 실패, 손상/절단 의심 (offset %d): %s"
                                 % (off, _short(exc)), path=path, segments=names)
    return Image(path, img, "ewf", segments)


def _last_ewf_section(path):
    with open(path, "rb") as fh:
        fh.seek(-76, os.SEEK_END)
        return fh.read(16).rstrip(b"\x00")


def _check_ewf_sequence(path, segments):
    exts = [os.path.splitext(s)[1][1:].upper() for s in segments]
    expected = [_ewf_ext(i) for i in range(1, len(exts) + 1)]
    if exts != expected:
        raise MissingSegmentError("E01 세그먼트 순서가 연속적이지 않습니다", path=path,
                                  found=exts, expected=expected)
    nxt = os.path.splitext(segments[-1])[0] + "." + _ewf_ext(len(exts) + 2)
    if os.path.exists(nxt):
        raise MissingSegmentError("E01 중간 세그먼트 누락", path=path,
                                  missing=_ewf_ext(len(exts) + 1))


def _ewf_ext(n):
    if n <= 99:
        return "E%02d" % n
    n -= 100
    return "E" + chr(ord("A") + n // 26) + chr(ord("A") + n % 26)


def _open_raw(path):
    segments = [path]
    m = _SPLIT_RAW_RE.match(path)
    if m:
        segments = []
        n = int(m.group("num"))
        while os.path.isfile("%s.%03d" % (m.group("stem"), n)):
            segments.append("%s.%03d" % (m.group("stem"), n))
            n += 1
        if os.path.isfile("%s.%03d" % (m.group("stem"), n + 1)):
            raise MissingSegmentError("분할 RAW 중간 세그먼트 누락", path=path,
                                      missing="%s.%03d" % (os.path.basename(m.group("stem")), n))
    if os.path.getsize(path) == 0:
        raise ImageOpenError("이미지 크기가 0 입니다", path=path)
    try:
        img = pytsk3.Img_Info(path)
    except OSError as exc:
        raise ImageOpenError("RAW 이미지를 열 수 없습니다: %s" % _short(exc), path=path)
    return Image(path, img, "split-raw" if len(segments) > 1 else "raw", segments)


def _sha256_prefix(path, limit):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        data = fh.read(limit)
    h.update(data)
    return h.hexdigest(), len(data)


def _short(exc):
    return str(exc).splitlines()[0][:300]
