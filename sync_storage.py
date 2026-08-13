#!/usr/bin/env python3
"""
sync_storage.py — полноценный менеджер приватного хранилища VIBE-CODE
─────────────────────────────────────────────────────────────────────
Хранилище: B3B3097/Storage-VIBE-CODE (private)
  chats/            — транскрипты чатов  chat-<id>.json
  chats/index.json  — лёгкий индекс чатов
  keys/secrets.env  — API-ключи (KEY=value)
  backup/           — резервные копии файлов публичного репо
  .migrated_v1      — маркер одноразовой миграции

Команды:
  pull                     remote → local
  push [message]           local → remote
  sync                     pull + push
  status                   сводка по хранилищу
  doctor                   проверка токена / репо / git / прав
  chats list               индекс чатов
  chats get <id>           распечатать чат
  chats delete <id> [...]  удалить чат(ы)
  chats prune --keep N     оставить N последних
  keys list                ключи (маскированно)
  keys set K=V [...]       добавить/обновить ключи
  keys delete K [...]      удалить ключи
  keys import <file>       слить файл ключей (без дубликатов)
  migrate [--force] [--only chats|keys] [--source REPO]
                           первый запуск: перенос чатов из results/run-*
                           и локальных ключей; идемпотентно (маркер)
  backup [--source REPO]   сохранить копии файлов публичного репо

Env:
  GH_TOKEN, STORAGE_REPO, STORAGE_DIR, SOURCE_REPO, KEYS_FILE,
  MIGRATE_FORCE, VERBOSE
"""

import os
import sys
import json
import re
import time
import base64
import argparse
import datetime
import tempfile
import subprocess
import urllib.request
import urllib.error

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
GH_TOKEN      = os.getenv("GH_TOKEN", "")
STORAGE_REPO  = os.getenv("STORAGE_REPO", "B3B3097/Storage-VIBE-CODE")
STORAGE_DIR   = os.getenv("STORAGE_DIR", "/tmp/storage")
SOURCE_REPO   = os.getenv("SOURCE_REPO", "B3B3097/VIBE-CODE")
KEYS_FILE     = os.getenv("KEYS_FILE", "")
MIGRATE_FORCE = os.getenv("MIGRATE_FORCE", "false").lower() == "true"
VERBOSE       = os.getenv("VERBOSE", "false").lower() == "true"

API_BASE   = "https://api.github.com"
MARKER     = ".migrated_v1"
LOCK_FILE  = ".sync.lock"
LOCK_TTL   = 600          # сек — старше считается протухшим
MAX_RETRY  = 3
USER_AGENT = "vibe-storage/2.0"

BACKUP_FILES = [
    "token_usage.yaml",
    "README.md",
    "RELEASE_v1.0.md",
    "config.yaml",
]


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def log(msg: str):
    print(f"[storage] {msg}", flush=True)


def vlog(msg: str):
    if VERBOSE:
        print(f"[storage][v] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"[storage] 💥 {msg}", flush=True)
    sys.exit(code)


def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def now_stamp() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


# ─────────────────────────────────────────────────────────────────────────────
# LOCK (защита от параллельных sync в одном раннере)
# ─────────────────────────────────────────────────────────────────────────────
class Lock:
    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            try:
                age = time.time() - os.path.getmtime(self.path)
                if age > LOCK_TTL:
                    vlog(f"stale lock removed (age={int(age)}s)")
                    os.remove(self.path)
            except OSError:
                pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            die("another sync is running (lock exists). "
                f"Remove {self.path} if sure.")
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB API
# ─────────────────────────────────────────────────────────────────────────────
class GH:
    def __init__(self, token: str):
        self.token = token

    def req(self, method: str, path: str, data: dict = None,
            retries: int = MAX_RETRY) -> dict:
        body = json.dumps(data).encode() if data else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last_err = {}
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(f"{API_BASE}{path}", data=body,
                                         headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    out = json.loads(raw) if raw else {}
                    if isinstance(out, dict):
                        out["_scopes"] = r.headers.get("x-oauth-scopes", "")
                    return out
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                try:
                    msg = json.loads(e.read().decode()).get("message", e.reason)
                except Exception:
                    msg = e.reason
                last_err = {"error": msg, "code": code}
            except Exception as e:
                if attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                last_err = {"error": str(e), "code": 0}
        return last_err

    def ok(self, resp: dict) -> bool:
        return isinstance(resp, dict) and "error" not in resp

    def fetch_file(self, repo: str, path: str) -> str:
        resp = self.req("GET", f"/repos/{repo}/contents/{path}", retries=2)
        if self.ok(resp) and "content" in resp:
            return base64.b64decode(resp["content"]) \
                       .decode("utf-8", errors="replace")
        return ""

    def tree(self, repo: str) -> list:
        resp = self.req("GET",
                        f"/repos/{repo}/git/trees/HEAD?recursive=1")
        return [t.get("path", "") for t in resp.get("tree", [])] \
            if self.ok(resp) else []

    def me(self) -> dict:
        return self.req("GET", "/user")

    def repo_info(self, repo: str) -> dict:
        return self.req("GET", f"/repos/{repo}")

    def ensure_repo(self, repo: str) -> bool:
        info = self.repo_info(repo)
        if self.ok(info) and "full_name" in info:
            return True
        name = repo.split("/")[-1]
        created = self.req("POST", "/user/repos",
                           {"name": name, "private": True,
                            "auto_init": False})
        return self.ok(created) and "full_name" in created


# ─────────────────────────────────────────────────────────────────────────────
# GIT
# ─────────────────────────────────────────────────────────────────────────────
class Git:
    def __init__(self, directory: str, repo: str, token: str):
        self.dir  = directory
        self.repo = repo
        self.token = token

    def run(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", self.dir, *args],
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: "
                               f"{r.stderr.strip()[:300]}")
        return r

    @property
    def remote_url(self) -> str:
        if self.token:
            return (f"https://x-access-token:{self.token}"
                    f"@github.com/{self.repo}.git")
        return f"https://github.com/{self.repo}.git"

    def has_git(self) -> bool:
        return os.path.isdir(os.path.join(self.dir, ".git"))

    def has_commits(self) -> bool:
        return self.run("rev-parse", "HEAD", check=False).returncode == 0

    def identity(self):
        self.run("config", "user.name", "VIBE Storage Bot", check=False)
        self.run("config", "user.email",
                 "vibe-storage@users.noreply.github.com", check=False)

    def branch(self) -> str:
        r = self.run("symbolic-ref", "--short", "HEAD", check=False)
        return r.stdout.strip() or "main"

    def status_porcelain(self) -> list:
        r = self.run("status", "--porcelain", check=False)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def clone_or_open(self, gh: GH) -> str:
        """Вернуть строку-результат: 'updated' | 'cloned' | 'seeded' | 'local'."""
        os.makedirs(self.dir, exist_ok=True)

        if self.has_git():
            self.identity()
            self.run("remote", "set-url", "origin", self.remote_url,
                     check=False)
            r = self.run("pull", "--rebase", "origin", self.branch(),
                         check=False)
            if r.returncode != 0:
                self.run("pull", "--rebase", check=False)
            return "updated"

        r = subprocess.run(["git", "clone", self.remote_url, self.dir],
                           capture_output=True, text=True)
        if r.returncode != 0:
            vlog("clone failed, trying to create repo via API")
            gh.ensure_repo(self.repo)
            subprocess.run(["git", "clone", self.remote_url, self.dir],
                           capture_output=True, text=True)

        if self.has_git():
            self.identity()
            if not self.has_commits():
                self.seed()
                self.run("add", "-A")
                self.run("commit", "-m", "chore(storage): init structure",
                         check=False)
                self.run("push", "-u", "origin", "HEAD:main", check=False)
                return "seeded"
            return "cloned"

        # совсем не удалось — локальный режим
        self.seed()
        subprocess.run(["git", "init", "-b", "main", self.dir],
                       capture_output=True, text=True)
        self.identity()
        self.run("remote", "add", "origin", self.remote_url, check=False)
        return "local"

    def seed(self):
        os.makedirs(os.path.join(self.dir, "chats"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "keys"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "backup"), exist_ok=True)
        readme = os.path.join(self.dir, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write(
                    "# Storage-VIBE-CODE\n\n"
                    "Приватное хранилище VIBE-CODE.\n\n"
                    "- `chats/`           — транскрипты чатов\n"
                    "- `chats/index.json` — индекс чатов\n"
                    "- `keys/secrets.env` — API-ключи\n"
                    "- `backup/`          — копии файлов публичного репо\n\n"
                    "⚠️ PRIVATE. Не делать публичным, не форсить.\n")
        gi = os.path.join(self.dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as f:
                f.write("*.bak\n*.tmp\n.sync.lock\n")

    def commit_push(self, message: str) -> bool:
        self.identity()
        self.run("add", "-A")
        if self.run("diff", "--staged", "--quiet",
                    check=False).returncode == 0:
            return False
        self.run("commit", "-m", message)
        r = self.run("push", "-u", "origin", f"HEAD:main", check=False)
        if r.returncode != 0:
            self.run("push", "origin", "HEAD:main")
        return True


# ─────────────────────────────────────────────────────────────────────────────
# STORAGE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class Storage:
    def __init__(self):
        self.gh  = GH(GH_TOKEN)
        self.git = Git(STORAGE_DIR, STORAGE_REPO, GH_TOKEN)
        self.lock = Lock(os.path.join(STORAGE_DIR, LOCK_FILE))

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def chats_dir(self):  return os.path.join(STORAGE_DIR, "chats")
    @property
    def index_path(self): return os.path.join(self.chats_dir, "index.json")
    @property
    def keys_path(self):  return os.path.join(STORAGE_DIR, "keys",
                                              "secrets.env")
    @property
    def backup_dir(self): return os.path.join(STORAGE_DIR, "backup")
    @property
    def marker_path(self): return os.path.join(STORAGE_DIR, MARKER)

    # ── sync ─────────────────────────────────────────────────────────────────
    def pull(self):
        with self.lock:
            state = self.git.clone_or_open(self.gh)
            log(f"✅ pull: {state}")

    def push(self, message: str = ""):
        with self.lock:
            if not self.git.has_git():
                die("no git repo in STORAGE_DIR — run pull first")
            changes = self.git.status_porcelain()
            if not changes:
                log("✅ push: no changes")
                return
            msg = message or f"chore(storage): sync — {now_stamp()}"
            msg += f" ({len(changes)} file(s))"
            self.git.commit_push(msg)
            log(f"✅ push: {len(changes)} file(s) → {STORAGE_REPO}")

    def sync(self):
        self.pull()
        self.push()

    # ── chats ────────────────────────────────────────────────────────────────
    def chat_path(self, chat_id: str) -> str:
        return os.path.join(self.chats_dir, f"chat-{chat_id}.json")

    def load_chat(self, chat_id: str) -> dict:
        p = self.chat_path(chat_id)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def rebuild_index(self):
        os.makedirs(self.chats_dir, exist_ok=True)
        index = {}
        for fn in sorted(os.listdir(self.chats_dir)):
            if not fn.startswith("chat-") or not fn.endswith(".json"):
                continue
            cid = fn[5:-5]
            try:
                with open(os.path.join(self.chats_dir, fn),
                          encoding="utf-8") as f:
                    data = json.load(f)
                index[cid] = {
                    "prompt":  str(data.get("prompt", ""))[:120],
                    "created": data.get("created", ""),
                    "mode":    data.get("mode", ""),
                    "messages": len(data.get("messages", [])),
                    "migrated": bool(data.get("migrated_from")),
                }
            except Exception as e:
                vlog(f"index skip {fn}: {e}")
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        return index

    def chats_list(self):
        index = self.rebuild_index()
        if not index:
            log("chats: (empty)")
            return
        log(f"chats: {len(index)}")
        for cid, meta in index.items():
            log(f"  • {cid} | {meta['messages']} msg | "
                f"{meta['prompt'][:60] or '(no prompt)'}")

    def chats_get(self, chat_id: str):
        data = self.load_chat(chat_id)
        if not data:
            die(f"chat {chat_id} not found")
        print(json.dumps(data, ensure_ascii=False, indent=2))

    def chats_delete(self, ids: list):
        removed = 0
        for cid in ids:
            p = self.chat_path(cid)
            if os.path.exists(p):
                os.remove(p)
                removed += 1
                log(f"  🗑 deleted {cid}")
        if removed:
            self.rebuild_index()
        log(f"✅ deleted {removed} chat(s)")

    def chats_prune(self, keep: int):
        index = self.rebuild_index()
        if len(index) <= keep:
            log(f"✅ prune: nothing to do ({len(index)} <= {keep})")
            return
        ordered = sorted(index.items(),
                         key=lambda kv: kv[1].get("created", ""),
                         reverse=True)
        to_del = [cid for cid, _ in ordered[keep:]]
        self.chats_delete(to_del)

    # ── keys ─────────────────────────────────────────────────────────────────
    @staticmethod
    def mask(value: str) -> str:
        v = value.strip()
        if len(v) <= 8:
            return "****"
        return v[:4] + "*" * min(len(v) - 8, 24) + v[-4:]

    def _read_key_lines(self) -> list:
        if not os.path.exists(self.keys_path):
            return []
        with open(self.keys_path, encoding="utf-8") as f:
            return f.read().splitlines()

    def _write_key_lines(self, lines: list):
        os.makedirs(os.path.dirname(self.keys_path), exist_ok=True)
        with open(self.keys_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")

    def _parse_pairs(self, lines: list) -> dict:
        out = {}
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip()
        return out

    def keys_list(self):
        pairs = self._parse_pairs(self._read_key_lines())
        if not pairs:
            log("keys: (empty)")
            return
        log(f"keys: {len(pairs)}")
        for k, v in pairs.items():
            log(f"  🔑 {k} = {self.mask(v)}")

    def keys_set(self, pairs: list):
        lines = self._read_key_lines()
        existing = self._parse_pairs(lines)
        added = updated = 0
        for pair in pairs:
            if "=" not in pair:
                log(f"⚠️ skip (need K=V): {pair}")
                continue
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k in existing:
                if existing[k] != v:
                    lines = [f"{k}={v}" if ln.strip().startswith(k + "=")
                             else ln for ln in lines]
                    updated += 1
            else:
                lines.append(f"{k}={v}")
                existing[k] = v
                added += 1
        self._write_key_lines(lines)
        log(f"✅ keys set: +{added} added, {updated} updated")

    def keys_delete(self, names: list):
        lines = self._read_key_lines()
        removed = 0
        kept = []
        for ln in lines:
            key = ln.split("=", 1)[0].strip()
            if key in names:
                removed += 1
                continue
            kept.append(ln)
        self._write_key_lines(kept)
        log(f"✅ keys deleted: {removed}")

    def keys_import(self, path: str):
        if not os.path.exists(path):
            die(f"file not found: {path}")
        with open(path, encoding="utf-8", errors="replace") as f:
            new_lines = f.read().splitlines()
        existing = self._parse_pairs(self._read_key_lines())
        lines = self._read_key_lines()
        added = 0
        for ln in new_lines:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k = ln.split("=", 1)[0].strip()
            if k and k not in existing:
                lines.append(ln)
                existing[k] = "1"
                added += 1
        self._write_key_lines(lines)
        log(f"✅ keys imported from {path}: +{added}")

    # ── migrate ──────────────────────────────────────────────────────────────
    def migrate(self, force: bool, only: str, source: str):
        if os.path.exists(self.marker_path) and not force:
            log("✅ already migrated (marker exists) — skip "
                "(MIGRATE_FORCE=true to rerun)")
            return

        if not self.git.has_git():
            self.pull()

        log(f"🚚 migrate: {source} → {STORAGE_REPO}")
        n_chats = self._migrate_chats(source) if only in ("", "chats") else 0
        n_keys  = self._migrate_keys()        if only in ("", "keys")  else 0

        with open(self.marker_path, "w") as f:
            json.dump({"migrated_at": now_iso(), "source": source,
                       "chats": n_chats, "keys_added": n_keys}, f, indent=2)
        self.rebuild_index()
        log(f"✅ migration done: {n_chats} chats, {n_keys} keys")
        self.push("chore(storage): initial migration from " + source)

    def _parse_prompt(self, summary: str) -> str:
        m = re.search(r"\|\s*\*\*Prompt\*\*\s*\|\s*(.*?)\s*\|", summary)
        if m:
            return m.group(1).strip()
        m = re.search(r"Prompt\**:\s*(.+)", summary)
        return m.group(1).strip() if m else ""

    def _migrate_chats(self, source: str) -> int:
        paths = self.gh.tree(source)
        runs = sorted({m.group(1) for p in paths
                       if (m := re.match(r"^results/(run-\d+)/", p))})
        if not runs:
            log("⚠️ no results/run-* found in source")
            return 0

        os.makedirs(self.chats_dir, exist_ok=True)
        existing = set(os.listdir(self.chats_dir))
        moved = 0

        for rid in runs:
            if f"chat-{rid}.json" in existing:
                continue
            summary = self.gh.fetch_file(source,
                                         f"results/{rid}/_run_summary.md")
            reasoning_raw = self.gh.fetch_file(
                source, f"results/{rid}/_reasoning.json")
            notes = self.gh.fetch_file(source,
                                       f"results/{rid}/_release_notes.md")

            prompt = self._parse_prompt(summary) if summary else ""
            messages = []
            if prompt:
                messages.append({"ts": now_iso(), "role": "user",
                                 "model": "", "content": prompt})
            try:
                reasoning = json.loads(reasoning_raw) if reasoning_raw else []
            except Exception:
                reasoning = []
            for e in reasoning:
                messages.append({
                    "ts": now_iso(), "role": "assistant", "model": "",
                    "agent": e.get("agent", ""), "phase": e.get("phase", ""),
                    "tokens": e.get("tokens", 0),
                    "content": f"[{e.get('agent','?')} · "
                               f"{e.get('phase','?')}] {e.get('content','')}",
                })
            if notes:
                messages.append({"ts": now_iso(), "role": "assistant",
                                 "model": "", "agent": "release-notes",
                                 "content": notes[:5000]})
            if not messages:
                continue

            with open(self.chat_path(rid), "w", encoding="utf-8") as f:
                json.dump({"id": rid, "parent": "", "prompt": prompt,
                           "mode": "migrated", "created": now_iso(),
                           "migrated_from": source, "messages": messages},
                          f, ensure_ascii=False, indent=2)
            moved += 1
            log(f"  💬 {rid} ({len(messages)} msg)")
        return moved

    def _migrate_keys(self) -> int:
        candidates = ([KEYS_FILE] if KEYS_FILE else []) + [
            "secrets.env", "keys.env", ".env",
            "vibe/secrets.env", "keys/secrets.env"]
        src = next((c for c in candidates if c and os.path.exists(c)), None)
        if not src:
            log("ℹ️ local keys file not found — skip")
            return 0
        before = len(self._parse_pairs(self._read_key_lines()))
        self.keys_import(src)
        after = len(self._parse_pairs(self._read_key_lines()))
        return after - before

    # ── backup ──────────────────────────────────────────────────────────────
    def backup(self, source: str):
        os.makedirs(self.backup_dir, exist_ok=True)
        saved = 0
        for path in BACKUP_FILES:
            content = self.gh.fetch_file(source, path)
            if not content:
                continue
            dst = os.path.join(self.backup_dir,
                               path.replace("/", "__"))
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)
            saved += 1
            log(f"  📦 {path}")
        log(f"✅ backup: {saved} file(s) from {source}")
        self.push(f"chore(storage): backup from {source}")

    # ── diagnostics ──────────────────────────────────────────────────────────
    def status(self):
        n_chats = len([f for f in os.listdir(self.chats_dir)
                       if f.startswith("chat-")]) \
            if os.path.isdir(self.chats_dir) else 0
        n_keys = len(self._parse_pairs(self._read_key_lines()))
        log(f"repo     : {STORAGE_REPO}")
        log(f"dir      : {STORAGE_DIR} "
            f"(git={self.git.has_git()}, commits={self.git.has_commits()})")
        log(f"chats    : {n_chats}")
        log(f"keys     : {n_keys}")
        log(f"migrated : {os.path.exists(self.marker_path)}")
        log(f"pending  : {len(self.git.status_porcelain())} file(s)")

    def doctor(self):
        ok = True
        log("── doctor ──")
        log(f"python   : {sys.version.split()[0]}")
        git_ver = subprocess.run(["git", "--version"],
                                 capture_output=True, text=True)
        log(f"git      : {git_ver.stdout.strip() or 'NOT FOUND'}")
        ok &= git_ver.returncode == 0

        if not GH_TOKEN:
            log("token    : ❌ GH_TOKEN is empty")
            return
        me = self.gh.me()
        if self.gh.ok(me):
            log(f"token    : ✅ user={me.get('login')} "
                f"scopes=[{me.get('_scopes', '?')}]")
            if "repo" not in (me.get("_scopes") or ""):
                log("token    : ⚠️ scope 'repo' missing — private access "
                    "will fail")
                ok = False
        else:
            log(f"token    : ❌ {me}")
            ok = False

        info = self.gh.repo_info(STORAGE_REPO)
        if self.gh.ok(info):
            log(f"storage  : ✅ {info.get('full_name')} "
                f"private={info.get('private')}")
            if not info.get("private"):
                log("storage  : ⚠️ repo is PUBLIC — make it private!")
                ok = False
        else:
            log(f"storage  : ❌ {info.get('error')}")
            ok = False

        writable = os.access(os.path.dirname(STORAGE_DIR) or "/", os.W_OK)
        log(f"dir      : {'✅' if writable else '❌'} {STORAGE_DIR}")
        ok &= writable
        log("── doctor done ──")
        if not ok:
            sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sync_storage.py",
                                description="VIBE-CODE private storage")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("pull")
    sp = sub.add_parser("push")
    sp.add_argument("message", nargs="?", default="")
    sub.add_parser("sync")
    sub.add_parser("status")
    sub.add_parser("doctor")

    sp = sub.add_parser("chats")
    sp.add_argument("action", choices=["list", "get", "delete", "prune"])
    sp.add_argument("ids", nargs="*", default=[])
    sp.add_argument("--keep", type=int, default=50)

    sp = sub.add_parser("keys")
    sp.add_argument("action", choices=["list", "set", "delete", "import"])
    sp.add_argument("args", nargs="*", default=[])

    sp = sub.add_parser("migrate")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--only", choices=["chats", "keys"], default="")
    sp.add_argument("--source", default=SOURCE_REPO)

    sp = sub.add_parser("backup")
    sp.add_argument("--source", default=SOURCE_REPO)
    return p


def main():
    args = build_parser().parse_args()
    st = Storage()

    if args.cmd == "pull":
        st.pull()
    elif args.cmd == "push":
        st.push(args.message)
    elif args.cmd == "sync":
        st.sync()
    elif args.cmd == "status":
        st.status()
    elif args.cmd == "doctor":
        st.doctor()
    elif args.cmd == "chats":
        if args.action == "list":
            st.chats_list()
        elif args.action == "get":
            st.chats_get(args.ids[0] if args.ids else "")
        elif args.action == "delete":
            st.chats_delete(args.ids)
        elif args.action == "prune":
            st.chats_prune(args.keep)
    elif args.cmd == "keys":
        if args.action == "list":
            st.keys_list()
        elif args.action == "set":
            st.keys_set(args.args)
        elif args.action == "delete":
            st.keys_delete(args.args)
        elif args.action == "import":
            st.keys_import(args.args[0] if args.args else "")
    elif args.cmd == "migrate":
        st.migrate(force=args.force or MIGRATE_FORCE,
                   only=args.only, source=args.source)
    elif args.cmd == "backup":
        st.backup(args.source)
    else:
        st.status()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[storage] 🛑 interrupted")
        sys.exit(130)