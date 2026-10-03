"""
find_me.py - copy only the photos that contain YOUR face into a folder.

Everything runs on this laptop. No internet is used after the two model
files are downloaded. Nothing about anyone's face is saved to disk: the
numeric "fingerprints" live in memory and disappear when the script ends.
Your original photos are only ever COPIED, never moved or deleted.

Usage (from the facefinder folder):
    python find_me.py --refs refs --photos photos --out results

    refs    = folder with 3-5 clear photos of the person you're searching for
    photos  = folder with the photos to search (unzip the WhatsApp export here)
    out     = where results go (created for you)
"""

import argparse
import csv
import shutil
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Settings you can tune
# ---------------------------------------------------------------------------
DETECTOR_MODEL = "models/face_detection_yunet_2023mar.onnx"   # finds faces
RECOGNIZER_MODEL = "models/face_recognition_sface_2021dec.onnx"  # makes fingerprints

# SFace's cosine similarity: higher = more alike. OpenCV's docs suggest 0.363
# as the "same person" cut-off. We'll tune this using your own test results.
DEFAULT_THRESHOLD = 0.363
MAYBE_MARGIN = 0.10          # scores within this much BELOW the threshold -> "maybe" folder
DETECT_CONFIDENCE = 0.8      # how sure the detector must be that something is a face
MAX_SIDE = 1600              # shrink huge photos to this size (longest side) for speed

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}


# ---------------------------------------------------------------------------
# Loading images
# ---------------------------------------------------------------------------
def load_image(path: Path):
    """Read an image as a BGR array (what OpenCV uses). Returns None if unreadable."""
    suffix = path.suffix.lower()

    # iPhone originals are often HEIC, which OpenCV cannot open on its own.
    if suffix in {".heic", ".heif"}:
        try:
            import pillow_heif
            from PIL import Image, ImageOps

            pillow_heif.register_heif_opener()
            pil_img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        except Exception as e:
            print(f"  ! Could not open HEIC {path.name}: {e} (pip install pillow-heif)")
            return None

    # np.fromfile + imdecode works even if the path has non-English characters
    # (plain cv2.imread can fail on those on Windows). imdecode also applies
    # the phone's rotation info (EXIF), so sideways photos come out upright.
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def shrink(img):
    """Scale big photos down so detection is fast."""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= MAX_SIDE:
        return img
    scale = MAX_SIDE / longest
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
# Face detection + fingerprints
# ---------------------------------------------------------------------------
class FaceTools:
    def __init__(self):
        for model in (DETECTOR_MODEL, RECOGNIZER_MODEL):
            if not Path(model).exists():
                raise FileNotFoundError(
                    f"Missing model file: {model}. "
                    "Download both .onnx files into the 'models' folder."
                )
        # The detector draws boxes around faces. Input size is set per image.
        self.detector = cv2.FaceDetectorYN.create(
            DETECTOR_MODEL, "", (320, 320), score_threshold=DETECT_CONFIDENCE
        )
        # The recognizer turns an aligned face crop into a list of 128 numbers.
        self.recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER_MODEL, "")

    def faces_in(self, img):
        """Return a list of (box_row, fingerprint) for every face in the image."""
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(img)
        if faces is None:
            return []
        results = []
        for row in faces:
            # alignCrop straightens and crops the face the way SFace expects
            aligned = self.recognizer.alignCrop(img, row)
            fingerprint = self.recognizer.feature(aligned)
            results.append((row, fingerprint))
        return results

    def similarity(self, fp_a, fp_b):
        """Cosine similarity between two fingerprints (1.0 = identical)."""
        return self.recognizer.match(fp_a, fp_b, cv2.FaceRecognizerSF_FR_COSINE)


def list_images(folder: Path):
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXTENSIONS)


# ---------------------------------------------------------------------------
# Main steps
# ---------------------------------------------------------------------------
def build_reference_fingerprints(tools: FaceTools, refs_folder: Path):
    """From each reference photo, take the LARGEST face as 'the person'."""
    fingerprints = []
    for path in list_images(refs_folder):
        img = load_image(path)
        if img is None:
            print(f"  ! Could not read reference {path.name}")
            continue
        found = tools.faces_in(shrink(img))
        if not found:
            print(f"  ! No face found in reference {path.name} - use a clearer photo")
            continue
        # box row = [x, y, width, height, ...landmarks..., score]; largest = biggest w*h
        biggest = max(found, key=lambda f: f[0][2] * f[0][3])
        if len(found) > 1:
            print(f"  ~ {path.name} has {len(found)} faces; using the biggest. "
                  "Solo photos are safer.")
        fingerprints.append(biggest[1])
        print(f"  + reference ok: {path.name}")
    return fingerprints


def run_search(refs_folder, photos_folder, out_folder, threshold=DEFAULT_THRESHOLD,
               review_floor=None, progress=None, tools=None):
    """Search photos_folder for the face in refs_folder. Returns a dict of counts.

    progress(done, total) is called after every photo so a web page can show a bar.
    """
    refs_folder, photos_folder, out_folder = Path(refs_folder), Path(photos_folder), Path(out_folder)
    if review_floor is None:
        review_floor = threshold - MAYBE_MARGIN

    tools = tools or FaceTools()

    print("Learning the reference face...")
    reference_fps = build_reference_fingerprints(tools, refs_folder)
    if not reference_fps:
        raise ValueError("No usable face found in the reference photos. "
                         "Add 1-5 clear photos where the face is easy to see.")

    matched_dir = out_folder / "matched"
    maybe_dir = out_folder / "maybe"
    matched_dir.mkdir(parents=True, exist_ok=True)
    maybe_dir.mkdir(parents=True, exist_ok=True)

    photos = list_images(photos_folder)
    print(f"\nSearching {len(photos)} photos (auto-match >= {threshold}, review >= {review_floor:.2f})...")

    counts = {"total": len(photos), "unreadable": 0, "no_face": 0,
              "matched": 0, "maybe": 0, "no_match": 0}
    rows = []

    for i, path in enumerate(photos, start=1):
        img = load_image(path)
        if img is None:
            counts["unreadable"] += 1
            rows.append([path.name, "unreadable", "", 0])
        else:
            found = tools.faces_in(shrink(img))
            if not found:
                counts["no_face"] += 1
                rows.append([path.name, "no_face", "", 0])
            else:
                # Every face in the photo vs every reference; keep the best score.
                # (A group photo only needs ONE face to match.)
                best = max(tools.similarity(fp, ref) for _, fp in found for ref in reference_fps)
                if best >= threshold:
                    verdict, dest_dir = "matched", matched_dir
                elif best >= review_floor:
                    verdict, dest_dir = "maybe", maybe_dir
                else:
                    verdict, dest_dir = "no_match", None
                counts[verdict] += 1
                rows.append([path.name, verdict, f"{best:.3f}", len(found)])
                if dest_dir is not None:
                    # COPY with the score as a prefix; the original stays where it was.
                    shutil.copy2(path, dest_dir / f"{best:.2f}_{path.name}")
        if progress:
            progress(i, len(photos))
        elif i % 25 == 0 or i == len(photos):
            print(f"  {i}/{len(photos)} done")

    with open(out_folder / "results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "verdict", "best_score", "faces_found"])
        writer.writerows(rows)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Copy photos containing a given face.")
    parser.add_argument("--refs", required=True, help="folder of reference photos of the person")
    parser.add_argument("--photos", required=True, help="folder of photos to search")
    parser.add_argument("--out", default="results", help="output folder")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"auto-match cut-off (default {DEFAULT_THRESHOLD})")
    parser.add_argument("--review-floor", type=float, default=None,
                        help="lowest score that still goes to the 'maybe' folder "
                             f"(default: threshold minus {MAYBE_MARGIN})")
    args = parser.parse_args()

    for folder in (args.refs, args.photos):
        if not Path(folder).is_dir():
            raise SystemExit(f"Folder not found: {folder}")
    try:
        counts = run_search(args.refs, args.photos, args.out, args.threshold, args.review_floor)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))

    print("\n--- Summary ---")
    print(f"Photos scanned : {counts['total']}")
    print(f"Unreadable     : {counts['unreadable']}")
    print(f"No face found  : {counts['no_face']}")
    print(f"Matched        : {counts['matched']}   -> {Path(args.out) / 'matched'}")
    print(f"Maybe (review) : {counts['maybe']}   -> {Path(args.out) / 'maybe'}")
    print(f"Not you        : {counts['no_match']}")
    print(f"Details        : {Path(args.out) / 'results.csv'}")


if __name__ == "__main__":
    main()
