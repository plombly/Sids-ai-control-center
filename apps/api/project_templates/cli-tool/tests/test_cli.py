from {{PKG}} import main, word_count


def test_counts(tmp_path, capsys):
    assert word_count("a b\nc\n") == {"lines": 2, "words": 3, "characters": 6}
    path = tmp_path / "x.txt"
    path.write_text("one two\n")
    assert main([str(path)]) == 0
    assert capsys.readouterr().out.split()[:3] == ["1", "2", "8"]
