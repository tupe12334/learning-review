#!/usr/bin/env python3
"""One-time web review for an isolated Hermes session-learning run."""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
PLUGIN_DIR = Path(__file__).resolve().parent
JOB = Path(os.environ["LEARNING_REVIEW_JOB"]).resolve()
TOKEN = os.environ["LEARNING_REVIEW_TOKEN"]


def read_json(name: str, default):
    try:
        return json.loads((JOB / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(name: str, value):
    target = JOB / name
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def status():
    return read_json("status.json", {"state": "starting", "message": "Preparing the learning analysis…"})


def start_apply(selected):
    candidates = read_json("candidates.json", {"candidates": []}).get("candidates", [])
    allowed = {str(c.get("id")) for c in candidates}
    selected = [item for item in selected if item in allowed]
    if not selected:
        return False, "Select at least one lesson to apply."
    if status().get("state") == "applying":
        return False, "The approved learning run is already applying changes."
    write_json("selection.json", {"selected": selected, "ignored": sorted(allowed - set(selected)), "submitted_at": time.time()})
    write_json("status.json", {"state": "applying", "message": "An isolated agent is updating skills, committing, and pushing origin/main."})
    log = (JOB / "apply.log").open("w", encoding="utf-8")
    subprocess.Popen([sys.executable, str(PLUGIN_DIR / "learning_review_worker.py"), "apply", "--job", str(JOB)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return True, "Submitted. The isolated agent is applying only the lessons you selected."


PAGE = PLUGIN_DIR / "index.html"


class App(BaseHTTPRequestHandler):
    def log_message(self, format, *args): print(f"[learning-review] {format % args}", flush=True)
    def authorized(self): return parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN
    def json(self, code, payload):
        body=json.dumps(payload).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path=urlparse(self.path).path
        if not self.authorized(): return self.json(HTTPStatus.FORBIDDEN,{"error":"Invalid review link."})
        if path=="/":
            try:
                body=PAGE.read_bytes()
            except OSError:
                return self.json(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"Review UI asset is unavailable."})
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path=="/api/status": self.json(200,status())
        elif path=="/api/candidates": self.json(200,read_json("candidates.json",{"candidates":[]}))
        else: self.json(404,{"error":"Not found."})
    def do_POST(self):
        if not self.authorized(): return self.json(403,{"error":"Invalid review link."})
        if urlparse(self.path).path!="/api/submit": return self.json(404,{"error":"Not found."})
        try: data=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
        except json.JSONDecodeError: return self.json(400,{"error":"Invalid request."})
        ok,msg=start_apply(data.get("selected",[])); self.json(200 if ok else 400,{"ok":ok,"message":msg})


def tunnel(port):
    cloudflared = shutil.which("cloudflared")
    command = [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"] if cloudflared else [shutil.which("npx") or "npx", "--yes", "localtunnel", "--port", str(port)]
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    assert proc.stdout
    for line in proc.stdout:
        print(line.rstrip(),flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=0);args=p.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),App)
    port=server.server_address[1]
    threading.Thread(target=tunnel,args=(port,),daemon=True).start()
    server.serve_forever()
if __name__=='__main__':main()
