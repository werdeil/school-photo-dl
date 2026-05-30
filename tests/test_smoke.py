"""Tests fumigènes : import du package et de la CLI."""

from school_photo_dl.cli import build_parser
from school_photo_dl.shared.utils import safe_name


def test_package_importable():
    """Le package doit être importable et exposer __version__."""
    # pylint: disable=import-outside-toplevel  # import testé localement
    import school_photo_dl

    assert school_photo_dl.__version__


def test_cli_parser_builds():
    """Le parser argparse expose les sous-commandes tma et klassly."""
    parser = build_parser()
    args = parser.parse_args(["tma"])
    assert args.command == "tma"
    args = parser.parse_args(["klassly"])
    assert args.command == "klassly"


def test_safe_name():
    """safe_name remplace les caractères interdits par des underscores."""
    assert safe_name("hello/world") == "hello_world"
    assert safe_name('a:b"c|d') == "a_b_c_d"
