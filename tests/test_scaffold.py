import monograph


def test_package_imports() -> None:
    assert monograph.__name__ == "monograph"
