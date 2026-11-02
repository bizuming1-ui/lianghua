"""
持久化与备份管理——保证重新打开 Kaggle、重新运行整个系统后,账户 / 持仓 / 成交日志 / AI 复盘与教训都还在。

第一性原则:Kaggle 会话结束 = 本机磁盘消失,所以必须"写到会话之外"。三道保险:
  1) 云备份(推荐):GitHub 私有 Gist(只需一个仅勾选 gist 权限的 Token;你本来就在用 GitHub)。
       启动时自动按描述找到你的备份并恢复;运行中每次成交后 ~8 秒、平时每 60 秒(有变化时)自动上传。
  2) 本地文件:/kaggle/working/quant_state.json(原子写入)。点 Kaggle『Save Version』后,它会成为 Notebook 的 Output;
       下次把该 Output 作为 Input 挂载,启动时自动从 /kaggle/input/**/quant_state*.json 恢复。
  3) 手动:页面右上角『导出』/『导入』按钮(下载/上传 JSON)。
恢复时在所有来源里挑 saved_at 最新且有数据的一份。没有配置 Token 时,会明确提示"仅本会话有效"。
"""
import glob
import hashlib
import json
import os
import threading
import time
import urllib.request

GIST_DESC = "lianghua-quant-state (auto backup)"
GIST_FILE = "quant_state.json"
UA = {"User-Agent": "lianghua-quant", "Accept": "application/vnd.github+json"}


def _now_ms():
    return int(time.time() * 1000)


class Persist:
    def __init__(self, workdir, export_fn, import_fn):
        self.dir = workdir
        self.export_fn = export_fn
        self.import_fn = import_fn
        self.local_path = os.path.join(workdir, "quant_state.json")
        self.token = (os.environ.get("QUANT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
        self.gist_id = (os.environ.get("QUANT_GIST_ID") or "").strip()
        self.dirty = False
        self.urgent_at = 0.0
        self.last_hash = ""
        self.lock = threading.RLock()
        self.st = {"local": {"ok": False, "at": 0, "bytes": 0, "err": ""},
                   "cloud": {"enabled": bool(self.token), "ok": False, "at": 0, "gist": self.gist_id, "err": "", "kind": "GitHub Gist"},
                   "restored": {"from": "", "at": 0, "trades": 0, "detail": ""}}
        self.wake = threading.Event()

    # ---------- 工具 ----------
    def _gh(self, method, path, body=None, timeout=20):
        url = path if path.startswith("http") else "https://api.github.com" + path
        h = dict(UA)
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _raw(self, url):
        h = dict(UA)
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    @staticmethod
    def _digest(payload):
        p = dict(payload)
        p.pop("saved_at", None)
        return hashlib.md5(json.dumps(p, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _score(p):
        """有数据的备份才有恢复价值:成交数 + 决策数。"""
        if not isinstance(p, dict):
            return -1
        return len((p.get("sql") or {}).get("trades") or []) + len((p.get("ai") or {}).get("decisions_log") or [])

    # ---------- 云端 ----------
    def _find_gist(self):
        if self.gist_id:
            return self.gist_id
        for page in (1, 2, 3):
            lst = self._gh("GET", "/gists?per_page=100&page=%d" % page)
            for g in lst:
                if g.get("description") == GIST_DESC:
                    self.gist_id = g["id"]
                    return self.gist_id
            if len(lst) < 100:
                break
        return ""

    def _cloud_load(self):
        gid = self._find_gist()
        if not gid:
            return None
        g = self._gh("GET", "/gists/" + gid)
        f = (g.get("files") or {}).get(GIST_FILE)
        if not f:
            return None
        txt = f.get("content") if not f.get("truncated") else self._raw(f["raw_url"])
        return json.loads(txt)

    def _cloud_save(self, text):
        gid = self._find_gist()
        if not gid:
            g = self._gh("POST", "/gists", {"description": GIST_DESC, "public": False, "files": {GIST_FILE: {"content": text}}})
            self.gist_id = g["id"]
        else:
            self._gh("PATCH", "/gists/" + gid, {"files": {GIST_FILE: {"content": text}}})
        self.st["cloud"].update(gist=self.gist_id)

    # ---------- 本地 / Kaggle input ----------
    def _local_load(self):
        cands = [self.local_path, os.path.join(self.dir, "quant_state.json")]
        cands += glob.glob("/kaggle/input/**/quant_state*.json", recursive=True)
        best, src = None, ""
        for p in cands:
            try:
                with open(p, "r", encoding="utf-8") as fp:
                    d = json.load(fp)
                if self._score(d) >= 0 and (best is None or (d.get("saved_at", 0), self._score(d)) > (best.get("saved_at", 0), self._score(best))):
                    best, src = d, p
            except Exception:
                continue
        return best, src

    def _local_save(self, text):
        try:
            os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
            tmp = self.local_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                fp.write(text)
            os.replace(tmp, self.local_path)
            self.st["local"].update(ok=True, at=_now_ms(), bytes=len(text), err="")
        except Exception as ex:
            self.st["local"].update(ok=False, err=str(ex)[:120])

    # ---------- 启动恢复 ----------
    def boot_restore(self, db_fresh=True):
        """所有来源里取 saved_at 最新且有数据者;返回描述字符串。
        db_fresh=False(本机 SQLite 已有成交,更新鲜)时只恢复 AI 部分,不覆盖账户。"""
        cands = []
        d, src = self._local_load()
        if d:
            cands.append(("本地文件 " + src, d))
        if self.token:
            try:
                c = self._cloud_load()
                if c:
                    cands.append(("GitHub Gist " + (self.gist_id or "")[:8], c))
                self.st["cloud"].update(ok=True, err="")
            except Exception as ex:
                self.st["cloud"].update(ok=False, err="读取失败:%s" % str(ex)[:100])
        cands = [x for x in cands if self._score(x[1]) > 0]
        if not cands:
            return "没有找到可恢复的历史数据(全新开始)"
        cands.sort(key=lambda x: (x[1].get("saved_at", 0), self._score(x[1])), reverse=True)
        name, payload = cands[0]
        try:
            self.import_fn(payload if db_fresh else {k: v for k, v in payload.items() if k != "sql"})
            n = len((payload.get("sql") or {}).get("trades") or [])
            self.st["restored"] = {"from": name, "at": _now_ms(), "trades": n, "detail": "已恢复账户/持仓/%d 笔成交/AI 复盘与教训" % n}
            self.last_hash = self._digest(payload)
            return "已从【%s】恢复:%d 笔成交" % (name, n)
        except Exception as ex:
            self.st["restored"] = {"from": name, "at": 0, "trades": 0, "detail": "恢复失败:%s" % ex}
            return "恢复失败:%s" % ex

    # ---------- 运行期 ----------
    def mark_dirty(self, urgent=False):
        self.dirty = True
        if urgent:
            self.urgent_at = time.time()
            self.wake.set()

    def save_now(self, force=False):
        with self.lock:
            payload = self.export_fn()
            payload["saved_at"] = _now_ms()
            payload["version"] = 2
            h = self._digest(payload)
            if h == self.last_hash and not force:
                self.dirty = False
                return {"changed": False}
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self._local_save(text)
            if self.token:
                try:
                    self._cloud_save(text)
                    self.st["cloud"].update(ok=True, at=_now_ms(), err="")
                except Exception as ex:
                    self.st["cloud"].update(ok=False, err="上传失败:%s" % str(ex)[:100])
                    return {"changed": True, "cloud": False}
            self.last_hash = h
            self.dirty = False
            return {"changed": True, "cloud": bool(self.token)}

    def _loop(self):
        last_cloud = 0.0
        while True:
            self.wake.wait(5)
            self.wake.clear()
            try:
                if not self.dirty:
                    continue
                urgent = time.time() - self.urgent_at < 9 and self.urgent_at > 0
                if urgent and time.time() - self.urgent_at < 8:
                    time.sleep(max(0, 8 - (time.time() - self.urgent_at)))
                if time.time() - last_cloud >= (8 if self.urgent_at > last_cloud else 60):
                    self.save_now()
                    last_cloud = time.time()
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def status(self):
        s = json.loads(json.dumps(self.st))
        s["dirty"] = self.dirty
        s["tip"] = ("" if self.token else
                    "未配置云备份:仅本会话/本地文件有效。请在 Kaggle 的 Add-ons→Secrets 添加 GITHUB_TOKEN(仅勾选 gist 权限),或在 Cell 1 顶部填写 GITHUB_TOKEN,重启后数据才不会丢失。")
        return s
