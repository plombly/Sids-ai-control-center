"""Generated documentation stays in step with the code."""

import subprocess
import sys

from laika_testing import ROOT


def test_settings_reference_is_up_to_date():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/laika-docs.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_documented_page_exists():
    import re
    readme = (ROOT / "README.md").read_text()
    for link in re.findall(r"\]\((docs/[a-z-]+\.md)\)", readme):
        assert (ROOT / link).is_file(), link
