"""src/app, src/static 폴더를 ConfigMap YAML로 변환해 k8s/ 에 저장한다.

사용법: python src/build_configmaps.py
코드나 이미지를 수정한 뒤 다시 실행하고 `oc apply -f k8s/` 하면 된다.
"""
import os

SRC = os.path.dirname(os.path.abspath(__file__))
K8S = os.path.join(SRC, "..", "k8s")

TARGETS = [
    # (ConfigMap 이름, 원본 폴더, 출력 파일, 설명)
    ("smarthome-app", "app", "05-configmap-app.yaml", "Flask 앱 코드, HTML 템플릿, 추천 규칙"),
    ("smarthome-static", "static", "06-configmap-static.yaml", "CSS와 제품 예시 이미지(SVG)"),
]
EXTENSIONS = (".py", ".html", ".json", ".css", ".svg")


def build(name, folder, out_name, desc):
    src_dir = os.path.join(SRC, folder)
    files = sorted(f for f in os.listdir(src_dir) if f.endswith(EXTENSIONS))
    lines = [
        f"# {desc}",
        f"# 자동 생성 파일: src/{folder}/ 를 수정한 뒤 src/build_configmaps.py 를 실행하세요.",
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {name}",
        "  labels:",
        "    app: smarthome",
        "    app.kubernetes.io/part-of: smarthome",
        "data:",
    ]
    for fname in files:
        with open(os.path.join(src_dir, fname), encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n").rstrip("\n")
        lines.append(f"  {fname}: |")
        lines.extend(("    " + line) if line.strip() else "" for line in content.split("\n"))
    out = os.path.join(K8S, out_name)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    size = os.path.getsize(out)
    print(f"{out_name}: {len(files)} files, {size / 1024:.1f} KiB")
    assert size < 1024 * 1024, "ConfigMap은 1MiB를 넘을 수 없습니다."


if __name__ == "__main__":
    os.makedirs(K8S, exist_ok=True)
    for target in TARGETS:
        build(*target)
