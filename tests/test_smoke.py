"""Smoke test: pytest infrastructure is wired up."""


def test_periscope_package_importable():
    import periscope
    import periscope.routes
    assert periscope is not None
    assert periscope.routes is not None


def test_pyproject_testpaths_includes_tests():
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert "tests" in data["tool"]["pytest"]["ini_options"]["testpaths"]
