"""Runs the read-only WhatsApp helper (Node + Baileys) and exposes its events."""
import json, os, queue, subprocess, threading
from core import APP_DIR, base_dir


class Bridge:
    def __init__(self):
        self.q, self.p = queue.Queue(), None

    def start(self, watch=""):
        self.stop()
        node = os.path.join(base_dir(), "node", "node.exe")
        node = node if os.path.exists(node) else "node"
        script = os.path.join(base_dir(), "wa", "index.js")
        env = dict(os.environ, AUTH_DIR=os.path.join(APP_DIR, "wa_auth"), WATCH_GROUP=watch)
        try:
            with open(os.path.join(APP_DIR, "bridge.log"), "a", encoding="utf-8") as stderr:
                self.p = subprocess.Popen(
                    [node, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=stderr, text=True, encoding="utf-8",
                    cwd=os.path.join(base_dir(), "wa"), env=env,
                    creationflags=0x08000000 if os.name == "nt" else 0)
        except OSError as e:
            self.q.put({"type": "error", "msg": f"cannot start WhatsApp helper: {e}"}); return
        self.q.put({"type": "status", "msg": "WhatsApp helper started; waiting for its connection status..."})
        threading.Thread(target=self._read, args=(self.p,), daemon=True).start()

    def _read(self, p):
        for line in p.stdout:
            try: self.q.put(json.loads(line))
            except ValueError: pass
        if self.p is p:
            code = p.poll()
            self.p = None
            self.q.put({"type": "closed", "returncode": code})

    def send(self, **cmd):
        if self.p and self.p.poll() is None:
            try:
                self.p.stdin.write(json.dumps(cmd) + "\n"); self.p.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.q.put({"type": "error", "msg": f"WhatsApp helper command failed: {e}"})

    def stop(self):
        p, self.p = self.p, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
