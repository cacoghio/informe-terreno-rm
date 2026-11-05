"""Task 12: video de 30-60 s del visor (ffprobe + OCR con Tesseract sobre fotogramas reales)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from terreno.errors import RejectRequest
from terreno.video import render_video, serve_viewer, viewer_url

POC = Path(__file__).resolve().parent.parent
SAMPLE = POC / "viewer" / "sample" / "out" / "conclusivo"
TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
pytestmark = pytest.mark.skipif(
    not (SAMPLE / "manifest.json").exists() or not shutil.which("node") or not TESSERACT.exists(),
    reason="faltan la muestra del visor (make_sample.py), node o Tesseract",
)
SECONDS = 30
FPS = 8  # los tests usan pocos fps para ir rapido; el video real usa DEFAULT_FPS


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    out = tmp_path_factory.mktemp("video") / "recorrido.mp4"
    with serve_viewer() as base:
        url = viewer_url(base, "sample/out/conclusivo", fecha="2026-06-21")
        render_video(url, out, seconds=SECONDS, fps=FPS)
    return out


def _frame(video: Path, ss: float | None, dest: Path, vf: str | None = None, last: bool = False) -> Path:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    cmd += ["-sseof", "-0.3"] if last else ["-ss", str(ss)]
    cmd += ["-i", str(video), "-frames:v", "1"]
    if vf:
        cmd += ["-vf", vf]
    subprocess.run([*cmd, str(dest)], check=True)
    return dest


def _ocr(img: Path, psm: int = 6) -> str:
    r = subprocess.run([str(TESSERACT), str(img), "-", "--psm", str(psm)], capture_output=True, text=True, check=True)
    return r.stdout


def test_video_duration_between_30_and_60_s(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                        "-of", "json", str(video)], capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    assert 30.0 <= float(info["format"]["duration"]) <= 60.0
    st = info["streams"][0]
    assert st["codec_name"] == "h264" and (st["width"], st["height"]) == (1440, 900)


def test_video_has_confidence_card_last_frame(video, tmp_path):
    level = json.loads((SAMPLE / "manifest.json").read_text(encoding="utf-8"))["confidence"]["overall"]
    text = _ocr(_frame(video, None, tmp_path / "last.png", last=True))
    assert "GLO-30" in text.replace(" ", "")
    m = re.search(r"confianza\s+([ABC])\b", text)
    assert m and m.group(1) == level, f"nivel no legible en la tarjeta final:\n{text}"


def test_video_shows_exaggeration_label(video, tmp_path):
    # 75 % del recorrido: barrido de sombra con la exageracion elevada a x4,0; se recorta la pastilla "Exageracion xN"
    png = _frame(video, SECONDS * 0.75, tmp_path / "mid.png", vf="crop=240:44:530:834,scale=iw*4:ih*4:flags=lanczos")
    text = _ocr(png, psm=7)
    assert re.search(r"Exager\w+\W{0,4}\s*[x\u00d7X]?\s*4[.,]0", text), f"rotulo de exageracion no legible: {text!r}"


def test_video_rejects_out_of_range_duration(tmp_path):
    for bad in (10, 29, 61, 300):
        with pytest.raises(RejectRequest):
            render_video("http://127.0.0.1:1/index.html", tmp_path / "x.mp4", seconds=bad)
    assert not (tmp_path / "x.mp4").exists()
