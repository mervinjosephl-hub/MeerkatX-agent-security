"""One-off backfill: upload existing meerkatx/ reports to Azure Blob storage.

Mirrors app.py's build_report_zip_bytes()/upload_report_to_blob() rules
exactly (same zip contents, same container, same blob naming) without
importing app.py — it calls st.set_page_config() at module level, which
expects a real Streamlit runtime, not a plain script import.

Run with:
    uv run --with azure-storage-blob python3 streamlit_ui/backfill_blob.py

Requires AZURE_STORAGE_CONNECTION_STRING in the repo-root .env. Safe to
re-run — uploads overwrite by run name, and scan_runs.fileurl updates are
idempotent. Runs launched via the raw `strix` CLI (not through this
Streamlit app) have no scan_runs row to update — their report still gets
uploaded, just with no DB row to attach the URL to.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

from azure.storage.blob import BlobServiceClient

REPO_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_DIR / "meerkatx"
ENV_FILE = REPO_DIR / ".env"
DB_PATH = REPO_DIR / "streamlit_ui" / "users.db"
AZURE_CONTAINER_NAME = "reports"


def load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def build_report_zip_bytes(run_dir: Path) -> bytes | None:
    """Same three sources as app.py: report.md + vulnerabilities/*.md + sarif."""
    buf = io.BytesIO()
    wrote_anything = False
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        report_path = run_dir / "penetration_test_report.md"
        if report_path.exists():
            zf.writestr("penetration_test_report.md", report_path.read_bytes())
            wrote_anything = True

        vuln_dir = run_dir / "vulnerabilities"
        if vuln_dir.exists():
            for vf in sorted(vuln_dir.glob("*.md")):
                zf.writestr(f"vulnerabilities/{vf.name}", vf.read_bytes())
                wrote_anything = True

        sarif_path = run_dir / "findings.sarif"
        if sarif_path.exists():
            zf.writestr("findings.sarif", sarif_path.read_bytes())
            wrote_anything = True

    return buf.getvalue() if wrote_anything else None


def set_run_fileurl(run_name: str, fileurl: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "UPDATE scan_runs SET fileurl = ? WHERE run_name = ?", (fileurl, run_name)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main() -> None:
    env = load_env_file()
    conn_str = env.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        raise SystemExit("AZURE_STORAGE_CONNECTION_STRING not set in .env")
    if not RUNS_DIR.exists():
        raise SystemExit(f"No {RUNS_DIR} directory found")

    client = BlobServiceClient.from_connection_string(conn_str)
    container = client.get_container_client(AZURE_CONTAINER_NAME)
    try:
        container.create_container()
    except Exception:  # noqa: BLE001 - fine if it already exists
        pass

    run_dirs = sorted(d for d in RUNS_DIR.iterdir() if d.is_dir())
    if not run_dirs:
        print("No run directories to backfill.")
        return

    for run_dir in run_dirs:
        run_name = run_dir.name
        zip_bytes = build_report_zip_bytes(run_dir)
        if zip_bytes is None:
            print(f"SKIP  {run_name}  (no report/vulnerabilities/sarif to archive)")
            continue
        blob_client = container.upload_blob(name=f"{run_name}.zip", data=zip_bytes, overwrite=True)
        rows = set_run_fileurl(run_name, blob_client.url)
        db_note = "db updated" if rows else "no scan_runs row (CLI-launched, unowned)"
        print(f"OK    {run_name} -> {blob_client.url}  ({db_note})")


if __name__ == "__main__":
    main()
