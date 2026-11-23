"""Right-to-left display helper for Tk.

Tk on Windows hands each text line to the OS with a *left-to-right* base
direction, so Hebrew words come out right but punctuation, emoji, numbers and
brackets at the edges of a Hebrew run land on the wrong side ("…יעד בחר").
RLM/LRM marks do not help there.

``rtl(text)`` re-orders a logical Hebrew string at the *segment* level so that,
after the OS's own LTR-base layout, it reads as a proper RTL line.  Hebrew runs
are left in logical order (the OS reverses them itself); Latin/number runs are
untouched; only the neutral pieces sitting between different directions are
reversed and bracket-mirrored by us.
"""
from __future__ import annotations

import unicodedata

LRM = "‎"
_MIRROR = str.maketrans("()[]{}<>«»", ")(][}{><»«")
_JOINERS = {"‍", "️", "︎"}


def _is_rtl(ch: str) -> bool:
    o = ord(ch)
    return 0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF


def _cls(cluster: str) -> str:
    ch = cluster[0]
    if _is_rtl(ch):
        return "R"
    if ch.isalnum() and ord(ch) < 0x2000:
        return "L"
    return "N"


def _clusters(s: str) -> list[str]:
    out: list[str] = []
    for ch in s:
        if out and (ch in _JOINERS or out[-1][-1] == "‍"
                    or unicodedata.category(ch) in ("Mn", "Me")):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def rtl_line(line: str) -> str:
    if not any(_is_rtl(c) for c in line):
        return line
    cl = _clusters(line.replace(LRM, "").replace("‏", ""))
    kinds = [_cls(c) for c in cl]
    n = len(cl)
    # resolve neutrals: same strong type on both sides -> that type, else "E" (edge)
    i = 0
    while i < n:
        if kinds[i] != "N":
            i += 1
            continue
        j = i
        while j < n and kinds[j] == "N":
            j += 1
        # Line edges count as "different" so edge punctuation is always placed by us.
        left = kinds[i - 1] if i > 0 else "^"
        right = kinds[j] if j < n else "$"
        res = left if left == right else "E"
        for k in range(i, j):
            kinds[k] = res
        i = j
    segs: list[tuple[str, list[str]]] = []
    for c, k in zip(cl, kinds):
        if segs and segs[-1][0] == k:
            segs[-1][1].append(c)
        else:
            segs.append((k, [c]))
    out = []
    for k, cs in reversed(segs):
        if k == "E":
            out.append("".join(reversed(cs)).translate(_MIRROR))
        elif k == "L":
            out.append(LRM + "".join(cs) + LRM)   # keeps digits from re-attaching to Hebrew
        else:
            out.append("".join(cs))
    return "".join(out)


def rtl(text: str) -> str:
    """Make a (possibly multi-line) logical Hebrew string display correctly in Tk."""
    return "\n".join(rtl_line(ln) for ln in (text or "").split("\n"))


def wrap(text: str, measure, width: int) -> str:
    """Word-wrap *logical* text to ``width`` pixels (``measure(str) -> px``).

    Wrapping must happen before :func:`rtl`, otherwise Tk would wrap the
    already-reordered line and the end of the sentence would show up first.
    """
    lines: list[str] = []
    for para in (text or "").split("\n"):
        indent = para[: len(para) - len(para.lstrip(" "))]
        cur = ""
        for word in (w for w in para.split(" ") if w):
            cand = f"{cur} {word}" if cur else indent + word
            if cur and measure(cand) > width:
                lines.append(cur)
                cur = indent + word
            else:
                cur = cand
        lines.append(cur)
    return "\n".join(lines)
