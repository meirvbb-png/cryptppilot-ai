"""Run:  python -m app      (listens on 0.0.0.0:8000 by default)"""
import socket, sys
import uvicorn
from . import config as C


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((C.HOST, port)); return True
        except OSError:
            return False


def pick_port(want, tries=10):
    """The configured port, or the next free one if another program already uses it."""
    for p in range(want, want + tries):
        if port_free(p):
            return p
    return None


if __name__ == "__main__":
    try:                                          # Hebrew output on any Windows console code page
        sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    port = pick_port(C.PORT)
    if port is None:
        print(f"\n!! הפורטים {C.PORT}-{C.PORT + 9} תפוסים. סגור חלון קודם של התוכנה או שנה PORT בקובץ .env\n")
        sys.exit(2)
    if port != C.PORT:
        print(f"\n!! פורט {C.PORT} תפוס על ידי תוכנה אחרת, משתמש בפורט {port} במקומו.")
    C.PORT = port
    from . import backup
    backup.restore()                     # before the DB is opened (no-op without HF_TOKEN/HF_BACKUP_REPO)
    print("\n" + "=" * 60)
    print(f"  פתח בטלפון (באותה רשת Wi-Fi):  http://{lan_ip()}:{port}")
    print(f"  במחשב הזה:                     http://localhost:{port}")
    if not C.APP_TOKEN or C.APP_TOKEN.startswith("change-me"):
        print("  !! לא הוגדר APP_TOKEN אישי בקובץ .env - כל מי שברשת יכול להיכנס")
    print("=" * 60 + "\n")
    uvicorn.run("app.server:app", host=C.HOST, port=port, log_level="info")
