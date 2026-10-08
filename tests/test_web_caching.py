"""The dashboard's files must be revalidated by browsers (apps/web/nginx.conf)."""

from laika_testing import ROOT


def test_dashboard_files_are_never_kept_without_asking():
    conf = (ROOT / "apps/web/nginx.conf").read_text()
    block = conf[conf.index("    location / {"):]
    assert 'add_header Cache-Control "no-cache" always;' in block.split("}")[0]
