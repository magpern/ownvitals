"""Static guard (issue #301): the ECG PDF modules make no image or link calls, so a PDF can
never fetch a remote resource or carry a URI action. Synthetic sources only in the mutations."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "ownvitals" / "reports"
MODULES = ("ecg_pdf.py", "ecg_layout.py", "doctor_pdf.py", "doctor_layout.py")
BANNED = {
    "drawImage",
    "drawInlineImage",
    "drawImageSafe",
    "linkURL",
    "linkRect",
    "linkAbsolute",
    "bookmarkPage",
    "addOutlineEntry",
    "setCreator",  # no producer string that can carry a URL
    "ImageReader",
    "Image",
    "Flowable",
}
BANNED_IMPORTS = ("reportlab.platypus", "reportlab.lib.utils", "urllib", "requests", "httpx")


def violations(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in BANNED:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in BANNED:
            found.append(node.id)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(BANNED_IMPORTS):
            found.append(node.module or "")
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith(BANNED_IMPORTS)]
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(("http://", "https://"))
        ):
            found.append("url literal")
    return found


@pytest.mark.parametrize("name", MODULES)
def test_the_pdf_modules_make_no_image_or_link_calls(name: str) -> None:
    assert violations((SRC / name).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "planted",
    [
        "canvas.drawImage('x.png', 0, 0)",
        "c.drawInlineImage(img, 0, 0)",
        "canvas.linkURL('https://example.invalid', (0, 0, 1, 1))",
        "c.linkAbsolute('x', 'y', (0, 0, 1, 1))",
        "from reportlab.platypus import Image",
        "from reportlab.lib.utils import ImageReader",
        "import urllib.request",
        "u = 'https://example.invalid/a.png'",
    ],
)
def test_the_guard_fails_on_a_planted_image_or_link(planted: str) -> None:
    assert violations(planted) != []
