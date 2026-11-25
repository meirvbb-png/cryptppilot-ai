"""Optional backup of the bots' database + price cache to a private Hugging Face dataset.

Free Hugging Face Spaces have no persistent disk: on restart/sleep the container starts empty.
If HF_TOKEN and HF_BACKUP_REPO are set, the state is restored at start-up and uploaded every
10 minutes. Without them this module does nothing."""
import asyncio, logging, os, shutil, sqlite3, tempfile
from . import config as C

log = logging.getLogger("backup")
REPO = os.environ.get("HF_BACKUP_REPO", "")
TOKEN = os.environ.get("HF_TOKEN", "")
FILES = ["bots.db"] + [f"bars_{s}.parquet" for s in C.SYMBOLS]
STATUS = {"enabled": bool(REPO and TOKEN), "last_ok": None, "error": ""}


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=TOKEN)


def restore():
    """Download the last backup into DATA_DIR (before the DB is opened)."""
    if not STATUS["enabled"]:
        return
    from huggingface_hub import hf_hub_download
    try:
        _api().create_repo(REPO, repo_type="dataset", private=True, exist_ok=True)
        for f in FILES:
            try:
                p = hf_hub_download(REPO, f, repo_type="dataset", token=TOKEN)
                shutil.copy(p, C.DATA_DIR / f)
                log.info("restored %s", f)
            except Exception as e:
                log.info("no backup for %s (%s)", f, type(e).__name__)
    except Exception as e:
        STATUS["error"] = str(e)[:120]
        log.warning("restore failed: %s", e)


def upload(store=None):
    if not STATUS["enabled"]:
        return
    import time
    try:
        if store is not None:
            store.save()
        tmp = tempfile.mkdtemp()
        # consistent copy of the live SQLite DB
        src = sqlite3.connect(C.DB_PATH); dst = sqlite3.connect(os.path.join(tmp, "bots.db"))
        src.backup(dst); dst.close(); src.close()
        for f in FILES[1:]:
            if (C.DATA_DIR / f).exists():
                shutil.copy(C.DATA_DIR / f, os.path.join(tmp, f))
        _api().upload_folder(repo_id=REPO, repo_type="dataset", folder_path=tmp,
                             commit_message="bots state backup")
        shutil.rmtree(tmp, ignore_errors=True)
        STATUS.update(last_ok=time.time(), error="")
    except Exception as e:
        STATUS["error"] = str(e)[:120]
        log.warning("backup failed: %s", e)


async def loop(store):
    while STATUS["enabled"]:
        await asyncio.sleep(600)
        await asyncio.to_thread(upload, store)
