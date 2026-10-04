"""{{NAME}}: a command-line tool. Run it with: python3 -m {{PKG}} --help"""

import argparse
import sys


def word_count(text):
    """Lines, words and characters, like `wc`."""
    return {"lines": len(text.splitlines()), "words": len(text.split()), "characters": len(text)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="{{ID}}", description="{{NAME}}")
    parser.add_argument("files", nargs="*", help="files to count (default: standard input)")
    args = parser.parse_args(argv)
    texts = [open(path, encoding="utf-8").read() for path in args.files] or [sys.stdin.read()]
    for name, text in zip(args.files or ["-"], texts):
        counts = word_count(text)
        print(f"{counts['lines']:>7} {counts['words']:>7} {counts['characters']:>7} {name}")
    return 0
