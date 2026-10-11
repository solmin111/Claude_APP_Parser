import fnmatch
import hashlib
import os
import platform
import struct
from datetime import datetime, timedelta, timezone

import pytsk3

from . import bitlocker
from .errors import FilesystemError, LiveAccessError, UnsupportedFormatError, VolumeLockedError
from .locate import SCAN_ROOTS

READ_CHUNK = 1024 * 1024
TSK_ERRORS = (OSError, RuntimeError)
DIR = int(pytsk3.TSK_FS_META_TYPE_DIR)
REG = int(pytsk3.TSK_FS_META_TYPE_REG)


class VfsFile:

    def __init__(self, path, size, source, *, mft_record=None, sequence=None,
                 allocated=True, si_times=None, fn_times=None, volume_index=None,
                 volume_offset=None, native_path=None, parse_errors=None,
                 path_source=None, live_root=None, time_source=None, volume_drive=None):
        self.path = path
        self.size = size
        self._source = source
        self.mft_record = mft_record
        self.sequence = sequence
        self.allocated = allocated
        self.si_times = si_times or {}
        self.fn_times = fn_times or {}
        self.volume_index = volume_index
        self.volume_offset = volume_offset
        self.volume_drive = volume_drive
        self.native_path = native_path
        self.parse_errors = list(parse_errors or [])
        self.path_source = path_source
        self.live_root = live_root
        self.time_source = time_source
        self._cached = None

    def read(self):
        if self._cached is None:
            self._cached = self._source._read_file(self)
        return self._cached

    def sha256(self):
        return hashlib.sha256(self.read()).hexdigest()

    def location(self):
        loc = {"path": self.path, "size_bytes": self.size, "allocated": self.allocated}
        if self.path_source:
            loc["path_source"] = self.path_source
        if self.mft_record is not None:
            loc["mft_record"] = self.mft_record
            loc["mft_sequence"] = self.sequence
        if self.volume_index is not None:
            loc["partition_index"] = self.volume_index
            loc["partition_offset_bytes"] = self.volume_offset
        if self.volume_drive:
            loc["drive"] = self.volume_drive
        if self.native_path:
            loc["native_path"] = self.native_path
        if self.live_root:
            loc["live_root"] = self.live_root
        if self.time_source:
            loc["time_source"] = self.time_source
        if self.si_times:
            loc["ntfs_standard_information"] = self.si_times
        if self.fn_times:
            loc["ntfs_file_name"] = self.fn_times
        if self.parse_errors:
            loc["parse_errors"] = self.parse_errors
        return loc


def _abort_reason(volumes, locked):
    if volumes:
        return None
    return "locked" if locked else "unsupported"


class TskImageVfs:

    def __init__(self, image, key_material=None):
        img = image.img
        self.volumes = []
        self.scanned_dirs = {}
        self.skipped = []
        self.locked = []
        self._plain_images = []
        try:
            vs = pytsk3.Volume_Info(img)
            bs = vs.info.block_size
            scheme = {int(pytsk3.TSK_VS_TYPE_DOS): "MBR",
                      int(pytsk3.TSK_VS_TYPE_GPT): "GPT"}.get(int(vs.info.vstype),
                                                              str(vs.info.vstype))
            parts = [(p.start * bs, p.len * bs, p.desc.decode("utf-8", "replace"), scheme)
                     for p in vs if int(p.flags) & int(pytsk3.TSK_VS_PART_FLAG_ALLOC)]
        except TSK_ERRORS:
            parts = [(0, img.get_size(), "볼륨 이미지(파티션 테이블 없음)", "NONE")]

        for idx, (off, length, desc, scheme) in enumerate(parts):
            vd = {"index": idx, "scheme": scheme, "offset_bytes": off,
                  "length_bytes": length, "type": desc}
            fs_img, fs_off = img, off
            if bitlocker.is_bitlocker(img, off):
                vd["encryption"] = "BitLocker"
                try:
                    fs_img, vd["unlock_method"], vd["protectors"] = bitlocker.open_volume(
                        img, off, length, key_material)
                except VolumeLockedError as exc:
                    self.locked.append({**vd, "reason": exc.message, **exc.context})
                    self.skipped.append(self.locked[-1])
                    continue
                fs_off = 0
                self._plain_images.append(fs_img)
            try:
                fs = pytsk3.FS_Info(fs_img, offset=fs_off)
            except TSK_ERRORS as exc:
                self.skipped.append({**vd, "reason": "%s파일시스템 판독 불가: %s"
                                     % ("BitLocker 해제 후 " if "unlock_method" in vd else "",
                                        str(exc).splitlines()[0][:200])})
                continue
            if int(fs.info.ftype) != int(pytsk3.TSK_FS_TYPE_NTFS):
                self.skipped.append({**vd, "reason": "미지원 파일시스템: %s" % fs.info.ftype})
                continue
            vd.update(filesystem="NTFS", cluster_size=fs.info.block_size)
            self.volumes.append((vd, fs))

        reason = _abort_reason(self.volumes, self.locked)
        if reason == "locked":
            raise VolumeLockedError(self.locked[0]["reason"], volumes=self.locked)
        if reason == "unsupported":
            raise UnsupportedFormatError(
                "이미지에서 판독 가능한 NTFS 볼륨을 찾지 못했습니다",
                partitions=[{"offset_bytes": p[0], "type": p[2]} for p in parts],
                skipped=self.skipped)

        from . import mounted_devices
        try:
            self.drive_letters = mounted_devices.assign(
                img, parts[0][3], bs if parts[0][3] != "NONE" else 512, self.volumes)
        except Exception as exc:  # noqa: BLE001
            self.drive_letters = {"status": "드라이브 문자 판정 실패 (%s), C: 로 가정"
                                            % str(exc)[:100]}

    def iter_files(self):
        self.scanned_dirs = {}
        for vd, fs in self.volumes:
            dirs = {}
            self.scanned_dirs[vd["index"]] = dirs
            yielded = set()
            for path, entry in _expand_roots(fs):
                for vf in self._walk(vd, fs, path, entry, dirs):
                    yielded.add(vf.mft_record)
                    yield vf
            yield from self._unlisted_deleted(vd, fs, dirs, yielded)

    def _walk(self, vd, fs, path, dir_entry, dirs):
        key = (dir_entry.info.meta.addr, dir_entry.info.meta.seq)
        if key in dirs:
            return
        dirs[key] = path
        try:
            directory = dir_entry.as_directory()
        except TSK_ERRORS:
            return
        for entry in directory:
            name = entry.info.name.name.decode("utf-8", "replace")
            meta = entry.info.meta
            if name in (".", "..") or meta is None:
                continue
            if int(meta.type) == DIR:
                yield from self._walk(vd, fs, path + "/" + name, entry, dirs)
            elif int(meta.type) == REG:
                yield self._vfile(vd, fs, path + "/" + name, entry)

    def _unlisted_deleted(self, vd, fs, dirs, yielded):
        bitmap = _mft_bitmap(fs)
        for inum in range(24, fs.info.last_inum + 1):
            if inum in yielded or (inum // 8 < len(bitmap) and bitmap[inum // 8] >> (inum % 8) & 1):
                continue
            try:
                entry = fs.open_meta(inode=inum)
            except TSK_ERRORS:
                continue
            meta = entry.info.meta
            if meta is None or int(meta.type) != REG:
                continue
            fn = _file_name_attr(entry)
            parent = dirs.get((fn.get("parent_mft"), fn.get("parent_seq")))
            if parent is not None:
                yield self._vfile(vd, fs, parent + "/" + fn["name"], entry, fn=fn,
                                  path_source="미할당 MFT 엔트리의 $FILE_NAME 부모 참조로 복원 "
                                              "(디렉터리 인덱스에 이름 없음)")

    def _vfile(self, vd, fs, path, entry, fn=None, path_source=None):
        meta, nm = entry.info.meta, entry.info.name
        name_alloc = nm is not None and bool(int(nm.flags) & int(pytsk3.TSK_FS_NAME_FLAG_ALLOC))
        meta_alloc = bool(int(meta.flags) & int(pytsk3.TSK_FS_META_FLAG_ALLOC))
        errors = []
        if nm is not None and not name_alloc and nm.meta_seq != meta.seq:
            errors.append("삭제된 이름이 가리키던 MFT 엔트리가 재할당됨 (seq %d -> %d), "
                          "내용이 다른 파일일 수 있음" % (nm.meta_seq, meta.seq))
        fn = fn if fn is not None else _file_name_attr(entry)
        vf = VfsFile(path=path, size=meta.size, source=self,
                     mft_record=meta.addr, sequence=meta.seq,
                     allocated=name_alloc and meta_alloc,
                     si_times=_si_times(meta), fn_times=fn.get("times"),
                     volume_index=vd["index"], volume_offset=vd["offset_bytes"],
                     volume_drive=vd.get("drive"),
                     parse_errors=errors, path_source=path_source)
        vf._fs = fs
        return vf

    def _read_file(self, vfile):
        out = bytearray()
        try:
            f = vfile._fs.open_meta(inode=vfile.mft_record)
            while len(out) < vfile.size:
                chunk = f.read_random(len(out), min(READ_CHUNK, vfile.size - len(out)))
                if not chunk:
                    break
                out += chunk
        except RuntimeError as exc:
            raise OSError("이미지에서 파일을 읽지 못함 (%d/%d 바이트): %s"
                          % (len(out), vfile.size, str(exc).splitlines()[0][:200])) from None
        if len(out) < vfile.size:
            raise OSError("파일 %d 바이트 중 %d 바이트만 읽힘 (클러스터 손상/덮어씀 가능)"
                          % (vfile.size, len(out)))
        return bytes(out)

    def describe(self):
        return {
            "reader": "The Sleuth Kit %s (pytsk3)" % pytsk3.TSK_VERSION_STR,
            "volumes": [vd for vd, _fs in self.volumes],
            "drive_letters": getattr(self, "drive_letters", None),
            "skipped_partitions": self.skipped,
            "locked_volumes": self.locked,
            "scan_roots": list(SCAN_ROOTS),
            "scan_note": ("SCAN_ROOTS 하위 디렉터리 열거(할당/삭제 이름) + 부모가 그 하위인 "
                          "미할당 MFT 엔트리. 부모 디렉터리까지 삭제·재할당된 파일은 경로 복원 불가"),
        }


def _mft_bitmap(fs):
    try:
        mft = fs.open_meta(inode=0)
        for attr in mft:
            if int(attr.info.type) == int(pytsk3.TSK_FS_ATTR_TYPE_NTFS_BITMAP):
                return mft.read_random(0, attr.info.size, attr.info.type, attr.info.id)
    except TSK_ERRORS:
        pass
    return b""


def _expand_roots(fs):
    for pattern in SCAN_ROOTS:
        level = [("", fs.open("/"))]
        for seg in pattern.lower().split("/"):
            nxt = []
            for path, dir_entry in level:
                try:
                    directory = dir_entry.as_directory()
                except TSK_ERRORS:
                    continue
                for entry in directory:
                    meta = entry.info.meta
                    name = entry.info.name.name.decode("utf-8", "replace")
                    if (meta is not None and name not in (".", "..")
                            and int(meta.type) == DIR
                            and fnmatch.fnmatch(name.lower(), seg)):
                        nxt.append((path + "/" + name, entry))
            level = nxt
        yield from level


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _si_times(meta):
    def ts(sec, nano):
        if not sec:
            return None
        return _iso(datetime.fromtimestamp(sec, tz=timezone.utc)
                    + timedelta(microseconds=nano // 1000))
    return {
        "created": ts(meta.crtime, meta.crtime_nano),
        "modified": ts(meta.mtime, meta.mtime_nano),
        "mft_modified": ts(meta.ctime, meta.ctime_nano),
        "accessed": ts(meta.atime, meta.atime_nano),
    }


def _file_name_attr(entry):
    found = []
    try:
        for attr in entry:
            if int(attr.info.type) != int(pytsk3.TSK_FS_ATTR_TYPE_NTFS_FNAME):
                continue
            raw = entry.read_random(0, attr.info.size, attr.info.type, attr.info.id)
            if len(raw) < 66:
                continue
            parent, c, m, r, a = struct.unpack_from("<QQQQQ", raw, 0)
            found.append({
                "parent_mft": parent & 0xFFFFFFFFFFFF,
                "parent_seq": parent >> 48,
                "namespace": raw[65],
                "name": raw[66:66 + raw[64] * 2].decode("utf-16-le", "replace"),
                "times": {"created": _filetime(c), "modified": _filetime(m),
                          "mft_modified": _filetime(r), "accessed": _filetime(a)},
            })
    except TSK_ERRORS:
        pass
    found.sort(key=lambda f: f["namespace"] == 2)
    return found[0] if found else {}


def _filetime(v):
    if not v:
        return None
    return _iso(datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=v // 10))


class DirectoryVfs:

    def __init__(self, root):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise FilesystemError("디렉터리가 아닙니다", path=self.root)

    def iter_files(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = "/" + os.path.relpath(full, self.root).replace(os.sep, "/")
                yield VfsFile(path=rel, size=st.st_size, source=self, native_path=full)

    def _read_file(self, vfile):
        with open(vfile.native_path, "rb") as fh:
            return fh.read()

    def describe(self):
        return {"filesystem": "host-directory", "root": self.root,
                "note": "디스크 이미지가 아닌 추출 파일 트리, MFT 근거 없음"}


def _stat_times(st):
    def ts(ns):
        if not ns:
            return None
        return _iso(datetime.fromtimestamp(0, tz=timezone.utc) + timedelta(microseconds=ns // 1000))
    birth = getattr(st, "st_birthtime_ns", None)
    if os.name == "nt":
        created = birth or st.st_ctime_ns
    else:
        created = birth
    return {"created": ts(created), "modified": ts(st.st_mtime_ns),
            "mft_modified": None, "accessed": ts(st.st_atime_ns)}


def _stat_mft_record(st):
    if os.name != "nt":
        return None
    return (st.st_ino & 0xFFFFFFFFFFFF) or None


def _long(path):
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def _is_link(entry):
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return False


_SHARING_VIOLATION = (32, 33)


def _open_shared(path):
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    h = k32.CreateFileW(path, generic_read, share_all, None, open_existing, normal, None)
    if h == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
    except OSError:
        k32.CloseHandle(h)
        raise
    return os.fdopen(fd, "rb")


def _is_sharing_violation(exc):
    return getattr(exc, "winerror", None) in _SHARING_VIOLATION


def _os_reason(exc):
    code = getattr(exc, "winerror", None)
    return "%s: %s%s" % (type(exc).__name__, exc.strerror or exc,
                         " (winerror %d)" % code if code else "")


class LiveVfs:

    TIME_SOURCE = "os.stat (실행 중인 파일시스템)"

    def __init__(self, root):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise LiveAccessError("라이브 분석 대상 폴더를 열 수 없습니다", path=self.root)
        drive, tail = os.path.splitdrive(self.root)
        self.prefix = self.root.rstrip("\\/") if tail.strip("\\/") else drive.upper()
        self.denied = []
        self.read_failures = []

    def _roots(self):
        for pattern in SCAN_ROOTS:
            level = [("", self.root)]
            for seg in pattern.lower().split("/"):
                nxt = []
                for rel, full in level:
                    try:
                        entries = list(os.scandir(_long(full)))
                    except OSError as exc:
                        if rel:
                            self._deny(rel, exc)
                        continue
                    for e in entries:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False) and not _is_link(e)
                        except OSError:
                            continue
                        if is_dir and fnmatch.fnmatch(e.name.lower(), seg):
                            nxt.append((rel + "/" + e.name, os.path.join(full, e.name)))
                level = nxt
            yield from level

    def _deny(self, rel, exc):
        if any(d["path"] == rel for d in self.denied):
            return
        self.denied.append({"path": rel, "reason": _os_reason(exc)})

    def iter_files(self):
        seen = set()
        for rel_root, full_root in self._roots():
            stack = [(rel_root, full_root)]
            while stack:
                rel, full = stack.pop()
                key = os.path.normcase(full)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    entries = sorted(os.scandir(_long(full)), key=lambda e: e.name)
                except OSError as exc:
                    self._deny(rel, exc)
                    continue
                for e in entries:
                    child_rel, child_full = rel + "/" + e.name, os.path.join(full, e.name)
                    try:
                        if _is_link(e):
                            continue
                        if e.is_dir():
                            stack.append((child_rel, child_full))
                            continue
                        if not e.is_file():
                            continue
                        st = e.stat()
                    except OSError as exc:
                        self._deny(child_rel, exc)
                        continue
                    yield VfsFile(path=child_rel, size=st.st_size, source=self,
                                  native_path=child_full,
                                  mft_record=_stat_mft_record(st),
                                  si_times=_stat_times(st), live_root=self.prefix,
                                  time_source=self.TIME_SOURCE)

    def _read_file(self, vfile):
        full = os.path.join(self.root, vfile.path.lstrip("/").replace("/", os.sep))
        try:
            with _open_shared(_long(full)) as fh:
                return fh.read()
        except OSError as exc:
            self.read_failures.append({"path": vfile.path, "reason": _os_reason(exc),
                                       "locked": _is_sharing_violation(exc)})
            raise

    def describe_input(self):
        elevated = None
        if os.name == "nt":
            try:
                import ctypes
                elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except (AttributeError, OSError):
                pass
        return {
            "image_id": "live:%s:%s" % (platform.node(), self.root),
            "image_type": "live",
            "file_name": self.root,
            "hostname": platform.node(),
            "os": platform.platform(),
            "collected_at_utc": _iso(datetime.now(timezone.utc).replace(microsecond=0)),
            "elevated": elevated,
            "note": ("실행 중인 시스템의 파일을 직접 읽음, 디스크 이미지가 아니므로 같은 결과를 "
                     "다시 만들 수 없고, 읽는 동안 앱이 파일을 바꿀 수 있다. 원본은 읽기 전용으로만 "
                     "열었다 (마지막 접근 시각은 운영체제 설정에 따라 갱신될 수 있음)"),
        }

    def describe(self):
        return {"filesystem": "live", "root": self.root,
                "scan_roots": list(SCAN_ROOTS),
                "denied": self.denied,
                "read_failures": self.read_failures,
                "note": ("실행 중인 시스템의 파일을 직접 읽음, 시각·MFT 번호는 os.stat 값, "
                         "삭제 파일·$UsnJrnl 없음")}
