import re

from .. import events, model
from ..util import excerpt
from .misc import _BY_MARKER, _MARKER_RE
from .v8text import SKIP_FILES, TEXT_RUN, is_human_text, messages, views

ROOT_PARENT = "00000000-0000-4000-8000-000000000000"

DRAFT_ANCHORS = ("chorus-unified-composer", "store:chat-draft:")
DRAFT_WINDOW = 1024
DRAFT_CAP = 200

_SKIP_PREFIX = ("store:", "chorus-", "keyval", "http", "blob:", "data:")
_TEMP_CHAT = re.compile(r'"?is_temporary"?\s*[:=]\s*true', re.I)
_MODEL = re.compile(r'"?model"?\s*[:=]\s*"?(claude[-\w.]{3,40})', re.I)
_TITLE = re.compile(r'"?(?:name|title)"?\s*[:=]\s*"([^"]{2,120})"')


def _draft_texts(data):
    found, seen = [], set()
    for anchor in DRAFT_ANCHORS:
        needle = anchor.encode("utf-16-le")
        pos = data.find(needle)
        while pos != -1 and len(seen) < DRAFT_CAP:
            chunk = data[pos: pos + DRAFT_WINDOW]
            for enc, text, shift in views(chunk):
                if enc == "utf-8":
                    continue
                for m in TEXT_RUN.finditer(text):
                    s = m.group(0).strip()
                    if len(s) < 6 or s in seen or s.startswith(_SKIP_PREFIX):
                        continue
                    if not is_human_text(s):
                        continue
                    seen.add(s)
                    found.append((s, pos + m.start() * 2 + shift, anchor))
            pos = data.find(needle, pos + 1)
    return _collapse(found)


def _collapse(found):
    found.sort(key=lambda t: -len(t[0]))
    kept = []
    for s, off, anchor in found:
        for i, (ks, koff, kanchor, n) in enumerate(kept):
            if ks.startswith(s):
                kept[i] = (ks, koff, kanchor, n + 1)
                break
        else:
            kept.append((s, off, anchor, 1))
    kept.sort(key=lambda t: t[1])
    return kept


LEVELDB_MAGIC = bytes.fromhex("57fb808b247547db")
STORAGE_SIGNS = (b"claude.ai", b"_chrome", b"META:", b"VERSION", LEVELDB_MAGIC,
                 b"chorus-", b"store:", b"conversation")


def is_storage_data(data):
    head = data[:2_000_000]
    tail = data[-65536:] if len(data) > 65536 else b""
    return any(sig in head or sig in tail for sig in STORAGE_SIGNS)


def parse(vfile, image, rb, max_excerpt=500):
    data = vfile.read()
    kind, _ = _kind_of(vfile.path)
    mtime = (vfile.si_times or {}).get("modified")
    out = []

    if not vfile.allocated and not is_storage_data(data):
        return [model.Record(
            rb.next_id(), "NA-00", events.name("NA-00"),
            model.timestamp(utc=mtime, source=model.TS_SOURCE_MFT,
                            null_reason=None if mtime else "파일 시각 없음"),
            model.user_action(observed="삭제된 %s 엔트리, 클러스터가 다른 데이터로 "
                                       "덮여 있어 내용을 이 아티팩트의 것으로 "
                                       "볼 수 없음" % kind),
            model.evidence(image, vfile, locator="deleted_entry_content_reused"),
            notes=["deleted_entry_cluster_reused"],
            details={"artifact": kind, "item_type": "deleted_entry_unreadable"})]

    low = vfile.path.replace(chr(92), '/').lower()
    skip_bodies = any(f in low for f in SKIP_FILES)
    for msg in ([] if skip_bodies else messages(data)):
        ts = model.timestamp(null_reason="V8 레코드에 메시지 시각이 남지 않음")
        user = msg["role"] == "user"
        known = msg["role_known"]
        if user:
            eid, act = _event_of(msg["text"])
            conf = model.CONF_CONFIRMED if msg["role_near"] else model.CONF_INFERRED
            observed = None
        else:
            eid, act = "NA-00", model.ACTION_UNKNOWN
            conf = model.CONF_UNKNOWN
            observed = ("모델 응답 본문" if known
                        else "대화 저장소의 본문, 역할 표식 없음")
        rationale = (
            "저장소 값에 역할 표식(%s)과 함께 본문이 남음, 표식 거리 %sB"
            % (msg["role"], msg["role_distance"]) if known else
            "대화 저장소에 본문이 남았으나 역할 표식이 없어 누가 한 말인지 미확인")
        out.append(model.Record(
            rb.next_id(), eid, events.name(eid), ts,
            model.user_action(act, conf, rationale=rationale, observed=observed),
            model.evidence(image, vfile,
                           locator="offset=%d(%s) role_distance=%s"
                                   % (msg["offset"], msg["encoding"],
                                      msg["role_distance"]),
                           excerpt=excerpt(msg["text"], max_excerpt)),
            details={"artifact": kind, "text": msg["text"], "role": msg["role"],
                     "conversation_uuid": msg["conversation_uuid"],
                     "role_distance": msg["role_distance"],
                     "role_known": known,
                     "item_type": "message"}))

    seen_uploads = set()
    for text, off, anchor, snaps in _draft_texts(data):
        ts = model.timestamp(utc=mtime, original=mtime, meaning="파일 수정 시각(대체)",
                             source=model.TS_SOURCE_MFT,
                             tz_note="초안 자체에 시각이 없어 파일 수정 시각으로 대체",
                             null_reason=None if mtime else "파일 시각 없음")
        up = _upload_of(text)
        if up:
            base = re.split(r"[\\/]", text.strip())[-1]
            if base.lower() in seen_uploads:
                continue
            seen_uploads.add(base.lower())
            eid, act = up
            out.append(model.Record(
                rb.next_id(), eid, events.name(eid), ts,
                model.user_action(act, model.CONF_INFERRED,
                                  rationale="입력창 초안에 업로드 대상 파일(%s) 경로/파일명만 "
                                            "남음, 첨부로 올린 파일로 보이나 전송은 미확인" % base),
                model.evidence(image, vfile,
                               locator="offset=%d(chat-draft upload)" % off,
                               excerpt=excerpt(text, max_excerpt)),
                details={"artifact": kind, "text": text, "role": "user",
                         "item_type": "upload_ref", "file_name": base,
                         "key": anchor, "snapshots": snaps,
                         "parent_message_uuid": ROOT_PARENT,
                         "conversation_uuid": "draft:%s" % vfile.path.rsplit("/", 1)[-1]}))
            continue
        out.append(model.Record(
            rb.next_id(), "E04-01", events.name("E04-01"), ts,
            model.user_action("사용자 프롬프트 입력/전송", model.CONF_INFERRED,
                              rationale="입력 초안, 사용자가 입력창에 친 문장이다. "
                                        "전송 여부는 확인되지 않았다 (타이핑 스냅샷 %d건)"
                                        % snaps),
            model.evidence(image, vfile, locator="offset=%d(chat-draft)" % off,
                           excerpt=excerpt(text, max_excerpt)),
            details={"artifact": kind, "text": text, "role": "user",
                     "item_type": "draft", "key": anchor, "snapshots": snaps,
                     "parent_message_uuid": ROOT_PARENT,
                     "conversation_uuid": "draft:%s" % vfile.path.rsplit("/", 1)[-1]}))

    head = data[:4_000_000].decode("utf-8", "replace")
    if _TEMP_CHAT.search(head):
        out.append(model.Record(
            rb.next_id(), "E10-01", events.name("E10-01"),
            model.timestamp(utc=mtime, source=model.TS_SOURCE_MFT,
                            null_reason=None if mtime else "파일 시각 없음"),
            model.user_action("임시 채팅 사용", model.CONF_INFERRED,
                              rationale="is_temporary 플래그"),
            model.evidence(image, vfile, locator="is_temporary"),
            details={"artifact": kind, "item_type": "temp_chat"}))
    mm = _MODEL.search(head)
    if mm:
        out.append(model.Record(
            rb.next_id(), "NA-00", events.name("NA-00"),
            model.timestamp(utc=mtime, source=model.TS_SOURCE_MFT,
                            null_reason=None if mtime else "파일 시각 없음"),
            model.user_action(observed="모델 식별자 %s" % mm.group(1)),
            model.evidence(image, vfile, locator="model"),
            details={"artifact": kind, "model": mm.group(1), "item_type": "model"}))
    return out


def _kind_of(path):
    from ..locate import classify
    return classify(path)


_ATTACH = re.compile(r"<uploaded_files>|첨부|업로드", re.I)
_IMAGE_REQ = re.compile(r"그려\s*줘|이미지.{0,6}(만들|생성)|그림.{0,6}(그려|만들)", re.I)
_DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|hwpx?|txt|csv)\b", re.I)
_IMG_EXT = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)\b", re.I)


_UPLOAD_REF = re.compile(
    r"^\S+\.(?P<ext>pdf|docx?|xlsx?|pptx?|hwpx?|txt|csv"
    r"|png|jpe?g|gif|webp|bmp|svg)$", re.I)


def _upload_of(text):
    s = (text or "").strip()
    if not s or len(s) > 260 or "\n" in s:
        return None
    m = _UPLOAD_REF.match(s)
    if not m:
        return None
    if m.group("ext").lower() in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"):
        return "E05-01", "이미지 업로드"
    return "E05-03", "문서 업로드"


def _event_of(text):
    m = _MARKER_RE.search(text)
    if m:
        hit = _BY_MARKER.get(m.group(1))
        if hit:
            return hit
    if _IMG_EXT.search(text) or ("사진" in text and _ATTACH.search(text)):
        return "E05-01", "이미지 업로드"
    if _DOC_EXT.search(text) or ("파일" in text and _ATTACH.search(text)):
        return "E05-03", "문서 업로드"
    if _IMAGE_REQ.search(text):
        return "E06-01", "이미지 생성 요청"
    return "E04-01", "사용자 프롬프트 입력/전송"
