import tailhedge


def test_package_imports_and_has_version():
    assert isinstance(tailhedge.__version__, str)
    assert tailhedge.__version__
