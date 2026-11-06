"""Flags report text that looks damaged by shell expansion.

When a report is written through an unquoted Bash heredoc, `inline code` is run as a
command and $VARS are expanded, so those spans vanish and leave stray spaces behind
(e.g. "故  無新增", "已使用 （未寫入"). Exit 1 and print suspects when any are found.

Usage: python3 -I check_report.py reports/2026/2026-11-04.md
"""
import re
import sys

CJK = r"[一-鿿]"
PATTERNS = [
    rf"\S {{2,}}{CJK}",   # two+ spaces before CJK, after a non-space (not indentation)
    rf"{CJK} {{2,}}\S",   # two+ spaces after CJK
    rf"{CJK} （",          # a space before a full-width paren, unusual in zh-TW
]


def main(path: str) -> int:
    suspects = []
    with open(path, encoding="utf-8") as f:
        for no, line in enumerate(f, 1):
            for pat in PATTERNS:
                for m in re.finditer(pat, line):
                    a, b = max(m.start() - 12, 0), m.end() + 12
                    suspects.append(f"line {no}: {line[a:b].strip()}")
                    break  # one hit per pattern per line is enough
    for s in suspects:
        print("suspect:", s)
    return 1 if suspects else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
