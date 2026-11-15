"""Read back what ``ownvitals.reports.ecg_pdf`` wrote, with no PDF library: the page
streams (ASCII85 then Flate), their strings and their path operations. Synthetic data only."""

import base64
import re
import zlib

PT_PER_MM = 72 / 25.4

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)


def streams(pdf: bytes) -> list[str]:
    """Each page content stream, decoded."""
    found: list[str] = []
    for match in _STREAM.finditer(pdf):
        raw = match.group(1).strip()
        raw = raw[:-2] if raw.endswith(b"~>") else raw
        try:
            data = zlib.decompress(base64.a85decode(raw))
        except (ValueError, zlib.error):  # not a page stream
            continue
        found.append(data.decode("latin-1"))
    return found


def text(pdf: bytes) -> str:
    """Every string a page shows, in order, joined by newlines."""
    out: list[str] = []
    for stream in streams(pdf):
        for shown in re.findall(r"\((.*?)(?<!\\)\) Tj", stream):
            out.append(re.sub(r"\\(.)", r"\1", shown))
    return "\n".join(out)


def media_box(pdf: bytes) -> tuple[float, float]:
    box = re.search(rb"/MediaBox \[ 0 0 ([\d.]+) ([\d.]+) \]", pdf)
    assert box is not None
    return float(box.group(1)), float(box.group(2))


def paths_mm(pdf: bytes) -> list[list[tuple[float, float]]]:
    """Every stroked path (``n ... m ... l ... S``) as points in millimetres, in drawing order."""
    found: list[list[tuple[float, float]]] = []
    for stream in streams(pdf):
        for body in re.findall(r"^n (.*?)\s*^S$", stream, re.M | re.S):
            pairs = re.findall(r"(-?[\d.]+) (-?[\d.]+) [ml]\b", body)
            found.append([(float(x) / PT_PER_MM, float(y) / PT_PER_MM) for x, y in pairs])
    return found
