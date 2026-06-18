from pathlib import Path


def test_internal_modules_import_brain_package_not_src_namespace():
    offenders = []
    for path in Path("src/brain").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "src.brain" in text:
            offenders.append(str(path))

    assert offenders == []
