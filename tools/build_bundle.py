"""Build the Elastic Beanstalk source bundle (dist/koreanpages-<stamp>.zip)
from django_app/ plus the directory data, leaving out development-only
files. Usage: python tools/build_bundle.py"""
import datetime
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "django_app"
SKIP_DIRS = {"__pycache__", "media", "backups", "staticfiles"}
SKIP_FILES = {"db.sqlite3", "directory/audit.py", "directory/audit_collector.js", "directory/management/commands/sync_live_suburbs.py"}


def main():
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "dist" / f"koreanpages-{stamp}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(APP.rglob("*")):
            rel = path.relative_to(APP).as_posix()
            if path.is_dir() or rel in SKIP_FILES or SKIP_DIRS & set(rel.split("/")[:-1]):
                continue
            if rel.endswith((".pyc", ".sqlite3")):
                continue
            z.write(path, rel)
        z.write(ROOT / "data" / "dev_directory.json.gz", "data/dev_directory.json.gz")
    print(out)


if __name__ == "__main__":
    main()
