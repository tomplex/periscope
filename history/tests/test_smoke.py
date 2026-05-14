def test_package_importable():
    import history
    assert hasattr(history, "index_one")
    assert hasattr(history, "search")


def test_cli_help(capsys):
    from history.cli import main
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "verbs:" in captured.out
