"""공식 문서 방향 모델을 고정된 리비전에서 받아 검증한다."""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / ".venv/ocr-models/PP-LCNet_x1_0_doc_ori"
REVISION = "d3b95a6dff5fe8a94f2748e12b61cb26818a0df8"
FILES = (
    ("inference.json", 104441, "git", "afade734133bc7a8df6c0f5a11efb4cc816c8c6b"),
    ("inference.yml", 766, "git", "be5ff4a823ed0dc4fb6fa8e228608ba9ab9fc01d"),
    ("inference.pdiparams", 6754166, "sha256", "e8d6e7c5d264507e40e58a655779059d616b20d7441ea22047d829eb3931989c"),
)


def verify(content, size, kind, expected):
    if len(content) != size:
        raise ValueError("모델 파일 크기가 일치하지 않습니다.")
    digest = (
        hashlib.sha1(f"blob {size}\0".encode() + content).hexdigest()
        if kind == "git" else hashlib.sha256(content).hexdigest()
    )
    if digest != expected:
        raise ValueError("모델 파일 검증 해시가 일치하지 않습니다.")


def main():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"model": "PP-LCNet_x1_0_doc_ori", "revision": REVISION, "files": []}
    for name, size, kind, expected in FILES:
        target = MODEL_DIR / name
        if target.exists():
            content = target.read_bytes()
            verify(content, size, kind, expected)
        else:
            url = f"https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori/resolve/{REVISION}/{name}?download=true"
            with urlopen(url, timeout=90) as response:
                content = response.read(size + 1)
            verify(content, size, kind, expected)
            temporary = target.with_name(name + ".part")
            temporary.write_bytes(content)
            temporary.replace(target)
        manifest["files"].append({"name": name, "bytes": size, "sha256": hashlib.sha256(content).hexdigest()})
        print(f"Verified: {name}", flush=True)

    (MODEL_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    print(f"Orientation model ready: {MODEL_DIR}")


if __name__ == "__main__":
    main()
