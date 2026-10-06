"""Start the Proof-Carrying Data Analyst:  python server.py [--port 8600]"""
import argparse

from app.api import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8600)
    a = ap.parse_args()
    serve(a.host, a.port)
