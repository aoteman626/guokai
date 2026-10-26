import argparse
from collections.abc import Sequence

from ouchn_assistant import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ouchn-assistant")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    return 0
