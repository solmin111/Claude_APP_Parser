import io
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

try:
    import zstandard
except ImportError:
    zstandard = None

from .. import events, model
from ..util import excerpt, parse_any_time

BEACON = re.compile(r"s-cdn\.anthropic\.com/images/[^\"'\s\x00]{0,1200}")
COWORK_ID = re.compile(r"/cowork/(cse_[0-9A-Za-z]{10,40})")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

PF_NAME = re.compile(r"^(?P<exe>[^-]+)-(?P<hash>[0-9A-F]{8})\.pf$", re.I)


ZSTD_MAGIC = bytes.fromhex("28b52ffd")
ZSTD_MAX = 8 * 1024 * 1024


def zstd_frames(data):
    if zstandard is None:
        return
    dctx = zstandard.ZstdDecompressor()
    pos = 0
    while True:
        i = data.find(ZSTD_MAGIC, pos)
        if i < 0:
            return
        pos = i + 4
        try:
            yield dctx.decompressobj().decompress(data[i:])
            continue
        except Exception:
            pass
        try:
            yield dctx.stream_reader(io.BytesIO(data[i:])).read(ZSTD_MAX)
        except Exception:
            continue


def _snapshot_records(blob, vfile, image, rb, max_excerpt):
    try:
        obj = json.loads(blob.decode("utf-8", "replace"))
    except Exception:
        return []
    items = obj if isinstance(obj, list) else [obj]
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("snapshot_name"):
            continue
        created = it.get("created_at")
        utc, fmt, note = parse_any_time(created)
        vis = it.get("visibility")
        out.append(model.Record(
            rb.next_id(), "E07-02", events.name("E07-02"),
            model.timestamp(utc=utc, original=created, fmt=fmt,
                            meaning="공유 스냅샷 생성 시각",
                            source=model.TS_SOURCE_CONTENT,
                            null_reason=None if utc else (note or "시각 해석 실패")),
            model.user_action("공유 링크 생성", model.CONF_CONFIRMED,
                              rationale="캐시에 남은 공유 스냅샷 목록에 생성 시각과 "
                                        "공개 범위(%s)가 기록됨" % vis),
            model.evidence(image, vfile, locator="zstd frame · share snapshot",
                           excerpt=excerpt(str(it.get("snapshot_name")), max_excerpt)),
            notes=["zstd_decompressed"],
            details={"artifact": "cache_block", "item_type": "share_snapshot",
                     "title": str(it.get("snapshot_name")),
                     "conversation_uuid": str(it.get("conversation_uuid") or ""),
                     "visibility": vis,
                     "snapshot_uuid": str(it.get("uuid") or "")}))
    return out


def parse_cache(vfile, image, rb, max_excerpt=500):
    data = vfile.read()
    text = data.decode("latin1", "replace")
    out, seen = [], set()
    if ZSTD_MAGIC in data:
        for blob in zstd_frames(data):
            if blob and b"snapshot_name" in blob:
                out.extend(_snapshot_records(blob, vfile, image, rb, max_excerpt))
    for m in BEACON.finditer(text):
        url = unquote(m.group(0))
        try:
            qs = parse_qs(urlparse("http://" + url).query)
        except ValueError:
            continue
        title = (qs.get("t") or [""])[0].strip()
        page = (qs.get("u") or [""])[0]
        cm = COWORK_ID.search(page)
        if not cm or not title:
            continue
        cid = "cowork:" + cm.group(1)
        if (cid, title) in seen:
            continue
        seen.add((cid, title))
        eid, act = _beacon_event(title, page)
        out.append(model.Record(
            rb.next_id(), eid, events.name(eid),
            model.timestamp(null_reason="비콘 URL 에 시각이 없음"),
            model.user_action(act, model.CONF_INFERRED,
                              rationale="분석 비콘에 대화 페이지 제목이 기록됨, "
                                        "제목은 앱이 잘라 보내므로 원문 일부다"),
            model.evidence(image, vfile, locator="beacon", excerpt=excerpt(url, max_excerpt)),
            details={"artifact": "cache_block", "conversation_uuid": cid,
                     "title": title, "item_type": "beacon"}))
    return out


_BY_MARKER = {
    "E0301": ("E03-01", "새로운 대화 생성"),
    "E0401": ("E04-01", "사용자 프롬프트 입력/전송"),
    "E0501": ("E05-01", "이미지 업로드"),
    "E0503": ("E05-03", "문서 업로드"),
    "E0601": ("E06-01", "이미지 생성 요청"),
    "E0701": ("E07-01", "대화 데이터 Export"),
    "E0702": ("E07-02", "공유 링크 생성"),
    "E0901": ("E09-01", "모델 변경"),
    "E1001": ("E10-01", "임시 채팅 사용"),
}
_MARKER_RE = re.compile(r"MRK_(E\d{4})")


def _beacon_event(title, page):
    m = _MARKER_RE.search(title)
    if m:
        hit = _BY_MARKER.get(m.group(1))
        if hit:
            return hit
    if "공유" in title or "/share" in page:
        return "E07-02", "공유 링크 생성"
    return "E03-01", "새로운 대화 생성"


def _parse_desktop_config(raw, vfile, image, rb, mtime, max_excerpt):
    try:
        obj = json.loads(raw.decode("utf-8-sig", "replace"))
    except (ValueError, UnicodeDecodeError):
        return [model.Record(
            rb.next_id(), "NA-00", events.name("NA-00"),
            model.timestamp(null_reason="JSON 해석 실패"),
            model.user_action(observed="claude_desktop_config.json 을 JSON 으로 읽지 못함"),
            model.evidence(image, vfile, locator="file"), status="partial",
            details={"artifact": "config", "key": vfile.path.rsplit("/", 1)[-1],
                     "item_type": "desktop_config"})]
    if not isinstance(obj, dict):
        return []
    prefs = obj.get("preferences") if isinstance(obj.get("preferences"), dict) else {}
    accounts = sorted({k for key, v in prefs.items()
                       if key.endswith("ByAccount") and isinstance(v, dict)
                       for k in v if UUID.fullmatch(str(k))})
    servers = obj.get("mcpServers") if isinstance(obj.get("mcpServers"), dict) else {}
    det = {"artifact": "config", "key": "claude_desktop_config.json",
           "item_type": "desktop_config",
           "mcp_servers": sorted(servers),
           "mcp_server_commands": {k: os_basename(v.get("command")) for k, v in servers.items()
                                   if isinstance(v, dict) and isinstance(v.get("command"), str)},
           "account_uuids": accounts,
           "remote_tools_device_name": prefs.get("remoteToolsDeviceName"),
           "cowork_user_files_path_set": bool(obj.get("coworkUserFilesPath")),
           "preference_keys": sorted(prefs)[:40]}
    ts = model.timestamp(utc=mtime, original=mtime, meaning="설정 파일 수정 시각",
                         source=model.TS_SOURCE_MFT,
                         null_reason=None if mtime else "파일 시각 없음")
    summary = "MCP 서버 %d개%s · 기본 설정 %d개" % (
        len(servers), (" (%s)" % ", ".join(sorted(servers)[:10])) if servers else "", len(prefs))
    out = [model.Record(
        rb.next_id(), "NA-00", events.name("NA-00"), ts,
        model.user_action(observed="claude_desktop_config.json, " + summary,
                          rationale="외부 도구 연결(MCP)·앱 설정 상태. 팀 행위 분류표에 "
                                    "대응 행위가 없어 관찰로만 남긴다"),
        model.evidence(image, vfile, locator="file", excerpt=excerpt(summary, max_excerpt)),
        details=det)]
    return out


def os_basename(cmd):
    return re.split(r"[\\/]", cmd.strip())[-1] if cmd else None


def parse_config(vfile, image, rb, max_excerpt=500):
    raw = vfile.read()
    mtime = (vfile.si_times or {}).get("modified")
    name = vfile.path.rsplit("/", 1)[-1]
    if name.lower() == "claude_desktop_config.json":
        return _parse_desktop_config(raw, vfile, image, rb, mtime, max_excerpt)
    det = {"artifact": "config", "key": name}
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        obj = None
    text = raw.decode("utf-8", "replace")
    ids = UUID.findall(text)
    if ids:
        det["account_uuid"] = ids[0]
        if len(ids) > 1:
            det["org_uuid"] = ids[1]
    out = []
    if isinstance(obj, dict):
        for k in ("model", "selectedModel", "lastModel"):
            if isinstance(obj.get(k), str):
                det["model"] = obj[k]
                break
    if name == "config.json":
        out.append(model.Record(
            rb.next_id(), "E08-02", events.name("E08-02"),
            model.timestamp(utc=mtime, original=mtime, meaning="설정 파일 재작성 시각",
                            source=model.TS_SOURCE_MFT,
                            null_reason=None if mtime else "파일 시각 없음"),
            model.user_action("정상 종료", model.CONF_INFERRED,
                              rationale="종료 직전 config.json 재작성"),
            model.evidence(image, vfile, locator="file"),
            details=dict(det, item_type="config_rewrite")))
    if ids:
        who = "계정 %s" % ids[0]
        if len(ids) > 1:
            who += " / 조직 %s" % ids[1]
        out.append(model.Record(
            rb.next_id(), "E01-01", events.name("E01-01"),
            model.timestamp(utc=mtime, original=mtime,
                            meaning="설정 파일 수정 시각",
                            source=model.TS_SOURCE_MFT,
                            null_reason=None if mtime else "파일 시각 없음"),
            model.user_action("로그인", model.CONF_INFERRED,
                              rationale="설정 파일에 계정·조직 식별자가 남아 있음 "
                                        ", 그 계정으로 로그인한 상태였다는 정황. "
                                        "인증 시각 자체는 아니다"),
            model.evidence(image, vfile, locator="file",
                           excerpt=excerpt(who, max_excerpt)),
            details=dict(det, item_type="identifiers", identifiers=who)))
    return out


def parse_prefetch(vfile, image, rb, max_excerpt=500):
    name = vfile.path.rsplit("/", 1)[-1]
    m = PF_NAME.match(name)
    exe = (m.group("exe") if m else name).upper()
    mtime = (vfile.si_times or {}).get("modified")
    setup = "SETUP" in exe
    eid = "E02-01" if setup else "E08-01"
    return [model.Record(
        rb.next_id(), eid, events.name(eid),
        model.timestamp(utc=mtime, original=mtime, meaning="프리패치 파일 수정 시각",
                        source=model.TS_SOURCE_MFT,
                        tz_note="마지막 실행 시각은 파일 내부에 있으나 압축 해제 필요",
                        null_reason=None if mtime else "파일 시각 없음"),
        model.user_action("애플리케이션 설치" if setup else "애플리케이션 실행",
                          model.CONF_INFERRED, rationale="윈도 프리패치에 실행 기록"),
        model.evidence(image, vfile, locator="prefetch"),
        details={"artifact": "prefetch", "file_name": name, "item_type": exe,
                 "lineage": "prefetch:%s" % name.upper()})]
