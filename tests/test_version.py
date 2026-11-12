"""The version is declared twice (packaging and `coded --version`/the welcome
card); a release must bump both."""

from __future__ import annotations

import re
from pathlib import Path

import coded


def test_package_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert coded.__version__ == declared
