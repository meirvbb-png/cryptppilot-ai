"""Run:  python -m app      (listens on 0.0.0.0:8000 by default)"""
import socket
import uvicorn
from . import config as C


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print(f"  פתח בטלפון (באותה רשת Wi-Fi):  http://{lan_ip()}:{C.PORT}")
    print(f"  במחשב הזה:                     http://localhost:{C.PORT}")
    print("=" * 60 + "\n")
    uvicorn.run("app.server:app", host=C.HOST, port=C.PORT, log_level="info")
