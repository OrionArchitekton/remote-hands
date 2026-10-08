import tomllib
from pathlib import Path

import remote_hands


def test_version_matches_pyproject():
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert remote_hands.__version__ == pyproject["project"]["version"]
