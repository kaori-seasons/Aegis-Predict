from pathlib import Path
from aegis.release import manifest


def test_release_manifest_hashes_source_without_capturing_environment(tmp_path, monkeypatch):
    (tmp_path/'src').mkdir()
    code = tmp_path/'src'/'x.py'; code.write_text('print(1)')
    (tmp_path/'.env').write_text('TOP_SECRET=do-not-export')
    monkeypatch.setenv('AEGIS_SECRET_KEYS', 'do-not-export')
    first = manifest(tmp_path)
    assert 'do-not-export' not in repr(first)
    assert len(first['source_tree_digest']) == 64
    code.write_text('print(2)')
    assert manifest(tmp_path)['source_tree_digest'] != first['source_tree_digest']
