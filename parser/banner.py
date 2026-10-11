from __future__ import annotations

import os
import sys

C = {
    "r": "\033[0m", "b": "\033[1m", "d": "\033[2m",
    "cy": "\033[36m", "CY": "\033[96m",
    "bl": "\033[34m", "BL": "\033[94m",
    "ye": "\033[33m", "YE": "\033[93m",
    "gr": "\033[32m", "GR": "\033[92m",
    "ma": "\033[35m", "MA": "\033[95m",
    "wh": "\033[37m", "WH": "\033[97m",
    "gy": "\033[90m",
}


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


def paint(text: str, enable: bool) -> str:
    for k, v in C.items():
        text = text.replace("{" + k + "}", v if enable else "")
    return text


def _b3() -> str:
    return "\n".join([
        "",
        "{d}{cy}»»» {r}{CY}███████ ██████  ███████ ██████  ███████ ███    ███{r}",
        "{d}{cy} »» {r}{CY}██      ██   ██ ██      ██   ██ ██      ████  ████{r}",
        "{d}{cy}  » {r}{cy}█████   ██████  █████   ██████  █████   ██ ████ ██{r}",
        "{d}{cy}    {r}{cy}██      ██   ██ ██      ██   ██ ██      ██  ██  ██{r}",
        "{d}{cy}    {r}{bl}███████ ██   ██ ███████ ██   ██ ███████ ██      ██{r}",
        "",
        "{b}{WH}      에 레 렘{r}{gy}  ·  C L A U D E   D E S K T O P   P A R S E R{r}",
        "",
    ])


BANNERS = {1: _b3}
DEFAULT = 1


def render(which: int = DEFAULT, color: bool | None = None) -> str:
    if color is None:
        color = supports_color()
    fn = BANNERS.get(which, BANNERS[DEFAULT])
    return paint(fn(), color).rstrip("\n")


def show(which: int = DEFAULT, color: bool | None = None) -> None:
    print(render(which, color))


if __name__ == "__main__":
    args = sys.argv[1:]
    color = True if "--color" in args else None
    picked = [int(a) for a in args if a.isdigit() and int(a) in BANNERS]

    if "--all" in args:
        for i in sorted(BANNERS):
            print("\n" + "─" * 72)
            print(f"  [{i}]")
            print("─" * 72)
            print(render(i, color=color))
        print()
    else:
        print(render(picked[0] if picked else DEFAULT, color=color))
