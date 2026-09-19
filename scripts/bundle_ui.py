"""Copy the built operator UI (ui/dist) into the package as agentos/_ui so the wheel — and the
image built from it — serve `/ui` with no Node at runtime (Phase 9).

    cd ui && npm ci && npm run build && cd .. && python scripts/bundle_ui.py

Refuses to bundle a stale or partial build: `ui/dist/index.html` must exist and reference every
asset it needs (a hashed `assets/*.js`), and a version stamp is written so `agentos doctor` and
the release smoke can say which UI version an install carries. `--check` verifies an existing
`agentos/_ui` matches `ui/dist` byte for byte (used by CI) without copying.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "ui" / "dist"
DST = ROOT / "agentos" / "_ui"


def _validate(src: Path) -> list[str]:
    index = src / "index.html"
    if not index.is_file():
        return [f"{index} missing — run `npm run build` in ui/ first"]
    html = index.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="/ui/(assets/[^"]+)"', html)
    problems = [f"index.html references {r} which is not in the build" for r in refs
                if not (src / r).is_file()]
    if not any(r.endswith(".js") for r in refs):
        problems.append("index.html references no script — not a Vite production build")
    return problems


def _version() -> str:
    pkg = json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))
    return str(pkg["version"])


def bundle(src: Path = SRC, dst: Path = DST) -> int:
    problems = _validate(src)
    if problems:
        print("refusing to bundle:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    (dst / "VERSION").write_text(_version() + "\n", encoding="utf-8")
    files = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
    print(f"bundled {len(files)} files into {dst.relative_to(ROOT)} (ui {_version()})")
    return 0


def check(src: Path = SRC, dst: Path = DST) -> int:
    if not (dst / "index.html").is_file():
        print(f"{dst} has no bundle", file=sys.stderr)
        return 1
    cmp = filecmp.dircmp(src, dst, ignore=["VERSION"])
    diff = cmp.left_only + cmp.right_only + cmp.diff_files
    if diff:
        print("agentos/_ui differs from ui/dist: " + ", ".join(sorted(diff)), file=sys.stderr)
        return 1
    print("agentos/_ui matches ui/dist")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify agentos/_ui matches ui/dist; copy nothing")
    args = ap.parse_args(argv)
    return check() if args.check else bundle()


if __name__ == "__main__":
    sys.exit(main())
