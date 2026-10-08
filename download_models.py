"""Download the two OpenCV Zoo models FaceGotcha needs into ./models.

Uses only the standard library, so it runs before/without any pip install.
Safe to run more than once: models that are already present are skipped.
"""
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"

# (filename, folder in OpenCV Zoo, minimum believable size in bytes)
MODELS = [
    ("face_detection_yunet_2023mar.onnx", "face_detection_yunet", 100_000),
    ("face_recognition_sface_2021dec.onnx", "face_recognition_sface", 30_000_000),
]

MODELS_DIR = Path(__file__).resolve().parent / "models"


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "facegotcha-setup"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total}%", end="", flush=True)
    print()


def main() -> int:
    MODELS_DIR.mkdir(exist_ok=True)

    for name, folder, min_size in MODELS:
        dest = MODELS_DIR / name

        if dest.exists() and dest.stat().st_size >= min_size:
            print(f"[ok] {name} already downloaded")
            continue

        url = f"{BASE}/{folder}/{name}"
        print(f"[..] Downloading {name}")
        try:
            download(url, dest)
        except Exception as e:
            dest.unlink(missing_ok=True)
            print(f"[error] Could not download {name}: {e}")
            print("        Check your internet connection and run setup again,")
            print("        or download it manually (see the README).")
            return 1

        if dest.stat().st_size < min_size:
            size = dest.stat().st_size
            dest.unlink(missing_ok=True)
            print(f"[error] {name} came out too small ({size} bytes).")
            print("        That usually means a pointer page was saved instead of the model.")
            return 1

        print(f"[ok] {name} saved")

    print("All models are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
