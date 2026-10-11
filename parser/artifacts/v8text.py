import re

TEXT_RUN = re.compile(r"[가-힣ㄱ-ㆎ -~·‘’“”...°]{6,}")

_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_WORD = re.compile(r"[A-Za-z]{2,}")
_KEYLIKE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.\-]{0,60}"?$')
_TSLIKE = re.compile(r'^[\d\-:.TZ+ ]{8,}"?$')

ROLE_NEAR = re.compile(r"role.{0,8}?(user|assistant|system|human)", re.I | re.S)
SENDER_NEAR = re.compile(r"sender.{0,8}?(human|assistant|user)", re.I | re.S)
SESSION_NEAR = re.compile(r"(cse_[0-9A-Za-z]{18,30})")
UUID_NEAR = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                       re.I)

UI_HINTS = ("<a ", "href=", "</", "ToolSearch", "connectors`", "Note: The set of",
            "개인정보 처리방침", "약관", "자세히 알아보",
            "your reply", "this step", "skill before", "description\"",
            "<svg", "xmlns", "http://", "https://")

NOISE_RE = re.compile(
    r"(\$\(|2>/dev/null|2>&1|\|\||&&\s|npm\s|node\s|python3?\s-c|NODE_PATH|"
    r"chmod|mkdir\s|wc\s-|\.exe\b|/opt/|/usr/|C:\\\\|"
    r"^\s*[\{\[]|\"ops\"|\"op\"\s*:|_status\b|Uuid\"|AtN?\b|CountI\b|"
    r"[A-Za-z0-9+/]{28,}={0,2}$)")

SKIP_FILES = ("indexeddb_blob/2/00/e",)

_LEAD_JUNK = re.compile(r'^[^가-힣A-Za-z\[(]{1,6}')
_TAIL_JUNK = re.compile(r'["\'\]\},:;\\]{1,6}$')


def views(data):
    return [("utf-8", data.decode("utf-8", "replace"), 0),
            ("utf-16le", data.decode("utf-16-le", "replace"), 0),
            ("utf-16le+1", data[1:].decode("utf-16-le", "replace"), 1)]


def is_human_text(s):
    if _KEYLIKE.match(s) or _TSLIKE.match(s):
        return False
    if "{" in s and "}" in s:
        return False
    if any(h in s for h in UI_HINTS):
        return False
    if len(_HANGUL.findall(s)) >= 4:
        return True
    return " " in s and len(s) >= 20 and len(_WORD.findall(s)) >= 4


NEAR_BYTES = 200


def attribute(ascii_view, start, end, window=900):
    lo, hi = max(0, start - window), end + window
    seg = ascii_view[lo:hi]
    hits = []
    for rx in (ROLE_NEAR, SENDER_NEAR):
        for m in rx.finditer(seg):
            pos = lo + m.start()
            dist = pos - end if pos >= end else start - pos
            v = m.group(1).lower()
            hits.append((abs(dist), "user" if v in ("user", "human") else v))
    role, dist, ambiguous = None, None, False
    if hits:
        hits.sort()
        dist, role = hits[0]
        ambiguous = len({v for _, v in hits}) > 1
    s = SESSION_NEAR.search(seg)
    u = UUID_NEAR.search(seg)
    return role, (s.group(1) if s else None), (u.group(1) if u else None), dist, ambiguous


def messages(data, min_len=10, cap=400):
    ascii_view = data.decode("utf-8", "replace")
    out, seen = [], set()
    for enc, text, shift in views(data):
        for m in TEXT_RUN.finditer(text):
            s = _LEAD_JUNK.sub("", m.group(0).strip()).strip('"\u0000 ').strip()
            s = _TAIL_JUNK.sub("", s).strip()
            if len(s) < min_len or s in seen or not is_human_text(s):
                continue
            if NOISE_RE.search(s):
                continue
            width = 2 if enc.startswith("utf-16") else 1
            start = m.start() * width + shift
            end = m.end() * width + shift
            role, sess, uuid, dist, ambiguous = attribute(ascii_view, start, end)
            if ambiguous:
                continue
            seen.add(s)
            out.append({"text": s, "role": role or "unknown", "offset": start,
                        "encoding": enc, "role_distance": dist,
                        "role_known": role is not None,
                        "role_near": bool(role) and dist is not None
                                     and dist <= NEAR_BYTES,
                        "conversation_uuid": ("cowork:" + sess) if sess else (uuid or "")})
            if len(out) >= cap:
                return out
    return out
