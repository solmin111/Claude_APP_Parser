import re

PACKAGE_FAMILY = "claude_pzs8sxrjxfjjc"

SCAN_ROOTS = (
    "Users/*/AppData/Local/Packages/" + PACKAGE_FAMILY,
    "Users/*/AppData/Roaming/Claude",
    "Users/*/AppData/Local/Claude",
    "Users/*/AppData/Local/Claude-Data",
    "Users/*/AppData/Local/AnthropicClaude",
    "Program Files/WindowsApps/claude_*",
    "ProgramData/Claude",
    "Windows/Prefetch",
)

_ROOT = (r"(?:"
         r"appdata/local/packages/" + PACKAGE_FAMILY + r"/localcache/roaming/claude"
         r"|appdata/roaming/claude"
         r"|appdata/local/claude-data"
         r"|appdata/local/claude"
         r"|appdata/local/anthropicclaude"
         r")")

_PATTERNS = [
    ("cache_block",   re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/cache/cache_data/(?P<fn>(data_\d+|f_[0-9a-f]+|index))$")),
    ("idb_leveldb",   re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/indexeddb/https_claude\.ai_\d+\.indexeddb\.leveldb/"
                                 r"(?P<fn>[^/]+\.(ldb|log)|CURRENT|MANIFEST-\d+)$", re.I)),
    ("idb_blob",      re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/indexeddb/https_claude\.ai_\d+\.indexeddb\.blob/(?P<rest>.+)$")),
    ("local_storage", re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/local storage/leveldb/(?P<fn>[^/]+\.(ldb|log))$", re.I)),
    ("session_storage", re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                   r"/session storage/(?P<fn>[^/]+\.(ldb|log))$", re.I)),
    ("config",        re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/(?P<fn>config\.json|ant-did|ant-device-registry\.json"
                                 r"|window-state\.json|plan-usage-history\.json"
                                 r"|claude_desktop_config\.json)$")),
    ("applog",        re.compile(r"/users/(?P<user>[^/]+)/appdata/local/claude/logs/"
                                 r"(?P<fn>[^/]+\.log)$")),
    ("applog_pkg",    re.compile(r"/users/(?P<user>[^/]+)/" + _ROOT +
                                 r"/logs/(?P<fn>[^/]+\.log)$")),
    ("applog_shared", re.compile(r"/programdata/claude/logs/(?P<fn>[^/]+\.log)$")),
    ("setuplog",      re.compile(r"/users/(?P<user>[^/]+)/appdata/local/temp/"
                                 r"(?P<fn>claudesetup\.log)$", re.I)),
    ("prefetch",      re.compile(r"/windows/prefetch/(?P<fn>claude[^/]*\.pf)$", re.I)),
    ("package_binary", re.compile(r"/program files/windowsapps/claude_(?P<ver>[^/]+)/"
                                  r"(?P<rest>.+)$")),
]

PRIMARY_KINDS = {"cache_block", "idb_leveldb", "idb_blob", "local_storage"}

SECONDARY_KINDS = {"config", "applog", "applog_pkg", "applog_shared", "setuplog",
                   "prefetch"}


def normalize(path):
    return path.replace("\\", "/").lower()


def classify(path):
    norm = normalize(path)
    for kind, rx in _PATTERNS:
        m = rx.search(norm)
        if m:
            return kind, {k: v for k, v in m.groupdict().items() if v is not None}
    return None, {}


OTHER_PRODUCTS = (
    ("/.claude/", "Claude Code(CLI)"),
    ("/.claude.json", "Claude Code(CLI)"),
    ("/google/chrome/", "Claude Web"),
    ("/microsoft/edge/", "Claude Web"),
    ("/openai/", "다른 앱"),
    ("/.codex/", "다른 앱"),
    ("/.lmstudio/", "다른 앱"),
)


def other_product(path):
    norm = normalize(path)
    for frag, name in OTHER_PRODUCTS:
        if frag in norm:
            return name
    return None
