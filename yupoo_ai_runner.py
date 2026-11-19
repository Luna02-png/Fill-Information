"""
YUPOO AI RUNNER v5 – KHÔNG dùng Web App.

Python đọc/ghi thẳng Google Sheet bằng Service Account:
  1. Trên sheet chọn dòng → menu 🤖 AI Yupoo → 🔄 Update information for selected rows
     (Apps Script ghi "⏳ Chờ" vào cột AI STATUS)
  2. Python thấy dòng "⏳" → mở Chrome (tự điền mật khẩu cột AY, chờ bạn xác thực captcha)
  3. Lấy tiêu đề + ảnh → Gemini (Google Search) → ghi Nickname, Style Code, Colorway,
     Release Date, Women/Men Size, link nguồn (cột ảnh Thumbnail), trạng thái ✅

Cài thư viện (1 lần):  pip install gspread google-auth requests playwright ddgs
                        python -m playwright install chromium
Chạy:                   python yupoo_ai_runner.py
Đổi cấu hình:           python yupoo_ai_runner.py --setup
"""
import base64
import glob
import json
import os
import re
import socket
import sys
import time
import unicodedata

import requests
import gspread
try:
    from ddgs import DDGS                      # pip install ddgs  (search DuckDuckGo miễn phí)
except Exception:
    try:
        from duckduckgo_search import DDGS
    except Exception:
        DDGS = None
from playwright.sync_api import sync_playwright

# ================== CẤU HÌNH MẶC ĐỊNH ==================
SPREADSHEET_ID = "1M3DRd-tEjMgOjsKouyKNcKccCAs0w5QsV282CUDcFwc"
SHEET_GID = 0                       # tab có gid=0 trong link sheet
CREDENTIAL_DIRS = [r"D:\Projects\kickscrew_credentials"]
SERVICE_EMAIL = "kickscrew-sheets-sync@data-crawler-512906.iam.gserviceaccount.com"
GEMINI_MODEL = ""                   # để trống = tự chọn bản Flash mới nhất Google đang mở
OPENROUTER_MODEL = "openrouter/free"  # bộ chọn model MIỄN PHÍ của OpenRouter (nhận cả ảnh)
GROQ_MODEL = ""                     # để trống = tự chọn model Groq xem được ảnh
OPENAI_MODEL = "gpt-5.1"            # ChatGPT – dự phòng 1 (đổi trong config.json: "openai_model")
CLAUDE_MODEL = "claude-sonnet-5-5"  # Claude – dự phòng 2 (đổi trong config.json: "claude_model")
PASSWORD_COL = "AY"                 # cột mật khẩu Yupoo
HEADER_ROW = 1
MAX_IMAGES = 5                      # 3 ảnh đầu + 2 ảnh cuối album
WAIT_VERIFY_SEC = 300               # chờ bạn xác thực tối đa 5 phút
POLL_SEC = 5                        # bao lâu đọc cột AI STATUS 1 lần
HEARTBEAT_SEC = 30
CLOSE_CHROME_IDLE = 120
GAP_SEC = 4
RETRY_SEC = 120
OVERWRITE = False                   # False = chỉ điền ô đang trống
LOCK_PORT = 47821
# =======================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "yupoo_profile")
LOG_FILE = os.path.join(BASE_DIR, "yupoo_ai.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LINK_MARK = "🔗 Nguồn:"

COLORS = {"⏳": "#fff2cc", "🔄": "#cfe2f3", "👉": "#f9cb9c", "✅": "#d9ead3", "⚠": "#f4cccc", "check": "#ffd966"}

HEADERS = {
    "brand": ["brand abbr"], "model": ["model key"], "nickname": ["nickname"],
    "style": ["style code"], "colorway": ["colorway"], "release": ["release date"],
    "women": ["women size"], "men": ["men size"],
    "og": ["og yupoo"], "norm": ["normal yupoo"],
    "thumb": ["anh thumbnail", "thumbnail"], "status": ["ai status"],
}

CFG = {}


# ======================= TIỆN ÍCH =======================
def log(*a):
    line = time.strftime("[%H:%M:%S] ") + " ".join(str(x) for x in a)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except Exception:
        pass


def norm_text(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d").replace("Đ", "D")
    return re.sub(r"\s+", " ", s).strip().lower()


def norm_url(u):
    return re.sub(r"^http:", "https:", str(u or "").strip(), flags=re.I)


def link_from(cell):
    """Lấy URL từ giá trị ô (dạng FORMULA): =HYPERLINK("url";...) hoặc chữ có URL."""
    s = str(cell or "")
    m = re.search(r'HYPERLINK\(\s*"([^"]+)"', s, re.I)
    if m:
        return norm_url(m.group(1))
    m = re.search(r"https?://\S+", s)
    return norm_url(m.group(0)) if m else ""


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + ord(ch) - 64
    return n


def hex_rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return s
    except OSError:
        return None


# ======================= CẤU HÌNH =======================
def find_credentials():
    cands = []
    for d in CREDENTIAL_DIRS + [BASE_DIR]:
        cands += glob.glob(os.path.join(d, "**", "*.json"), recursive=True)
    best = None
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        if j.get("type") == "service_account":
            if j.get("client_email") == SERVICE_EMAIL:
                return p
            best = best or p
    return best


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    setup = "--setup" in sys.argv
    if setup or not cfg.get("gemini_api_key"):
        print("\n=== CÀI ĐẶT (chỉ 1 lần) ===")
        k = input("Dán Gemini API key (aistudio.google.com/apikey): ").strip().strip('"')
        if k:
            cfg["gemini_api_key"] = k
    if setup or "openrouter_api_key" not in cfg:
        k = input("Dán OpenRouter API key MIỄN PHÍ (openrouter.ai → Keys) – dự phòng 1 (Enter để bỏ qua): ").strip().strip('"')
        cfg["openrouter_api_key"] = k or cfg.get("openrouter_api_key", "")
    if setup or "groq_api_key" not in cfg:
        k = input("Dán Groq API key MIỄN PHÍ (console.groq.com → API Keys) – dự phòng 2 (Enter để bỏ qua): ").strip().strip('"')
        cfg["groq_api_key"] = k or cfg.get("groq_api_key", "")
    if setup or "openai_api_key" not in cfg:
        k = input("Dán OpenAI (ChatGPT) API key – TRẢ PHÍ (Enter để bỏ qua): ").strip().strip('"')
        cfg["openai_api_key"] = k or cfg.get("openai_api_key", "")
    if setup or "claude_api_key" not in cfg:
        k = input("Dán Claude API key – TRẢ PHÍ (Enter để bỏ qua): ").strip().strip('"')
        cfg["claude_api_key"] = k or cfg.get("claude_api_key", "")
    if setup or not cfg.get("credentials") or not os.path.exists(cfg.get("credentials", "")):
        found = find_credentials()
        if found and not setup:
            cfg["credentials"] = found
        else:
            p = input(f"Đường dẫn file .json service account{(' (Enter = ' + found + ')') if found else ''}: ").strip().strip('"')
            cfg["credentials"] = p or found
    cfg.setdefault("openrouter_model", OPENROUTER_MODEL)
    cfg.setdefault("groq_model", GROQ_MODEL)
    cfg.setdefault("openai_model", OPENAI_MODEL)
    cfg.setdefault("claude_model", CLAUDE_MODEL)
    cfg.setdefault("spreadsheet_id", SPREADSHEET_ID)
    cfg.setdefault("sheet_gid", SHEET_GID)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    CFG.update(cfg)


# ======================= GOOGLE SHEET =======================
JOBS_FILE = os.path.join(BASE_DIR, "jobs.json")


def api(fn, *a, **k):
    """Gọi Google Sheets API; lỗi tạm thời (429/500/502/503/504, mạng) thì đợi rồi thử lại."""
    wait = 5
    for attempt in range(6):
        try:
            return fn(*a, **k)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            msg = str(e)
            transient = code in (429, 500, 502, 503, 504) or re.search(
                r"\b(429|500|502|503|504)\b|unavailable|timed out|Timeout|Connection", msg, re.I)
            if not transient or attempt == 5:
                raise
            log(f"   (Google Sheet bận – {msg[:60]} – đợi {wait}s thử lại)")
            time.sleep(wait)
            wait = min(wait * 2, 120)


class Sheet:
    def __init__(self):
        self.gc = gspread.service_account(filename=CFG["credentials"])
        self.ss = self.gc.open_by_key(CFG["spreadsheet_id"])
        self.ws = self.ss.get_worksheet_by_id(int(CFG["sheet_gid"]))
        self.gid = self.ws.id
        self.q = "'" + self.ws.title.replace("'", "''") + "'!"
        self.cols = {}
        self.cols_time = 0
        self.last_hb = 0
        self.last_pub = 0
        self.last_prog = ""
        self.recent_done = []
        self.jobs = {}          # dòng -> {"st": pending|running|done|check|error, "t": text, "url": link}
        try:
            with open(JOBS_FILE, encoding="utf-8") as f:
                self.jobs = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            self.jobs = {}

    def save_jobs(self):
        try:
            with open(JOBS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.jobs, f, ensure_ascii=False)
        except Exception:
            pass

    def job_state(self, text):
        if text.startswith("⏳"):
            return "pending"
        if text.startswith("🔄") or text.startswith("👉"):
            return "running"
        if text.startswith("✅"):
            return "check" if "⚠" in text else "done"
        if text.startswith("⚠"):
            return "error"
        return ""

    def refresh_cols(self, force=False):
        if not force and time.time() - self.cols_time < 300 and self.cols:
            return
        head = [norm_text(h) for h in api(self.ws.row_values, HEADER_ROW)]
        cols = {}
        for k, kws in HEADERS.items():
            for kw in kws:
                idx = next((i for i, h in enumerate(head) if kw in h), -1)
                if idx >= 0:
                    cols[k] = idx + 1
                    break
        for k in ("status", "og", "nickname"):
            if k not in cols:
                raise RuntimeError(f"Không thấy cột '{HEADERS[k][0]}' ở tab '{self.ws.title}'. "
                                   "Hãy bấm menu AI Yupoo 1 lần trên sheet để tạo cột AI STATUS.")
        self.cols, self.cols_time = cols, time.time()

    def batch_get(self, ranges, formula=False):
        params = {"valueRenderOption": "FORMULA" if formula else "FORMATTED_VALUE"}
        res = api(self.ss.values_batch_get, [self.q + r for r in ranges], params=params)
        out = []
        for vr in res.get("valueRanges", []):
            out.append(vr.get("values", []))
        return out

    def status_range(self, r1, r2):
        c = col_letter(self.cols["status"])
        vals = self.batch_get([f"{c}{r1}:{c}{r2}"])[0]
        return {r1 + i: (v[0] if v else "").strip() for i, v in enumerate(vals)}

    def status_column(self):
        c = col_letter(self.cols["status"])
        vals = self.batch_get([f"{c}{HEADER_ROW + 1}:{c}"])[0]
        return [(HEADER_ROW + 1 + i, (v[0] if v else "").strip()) for i, v in enumerate(vals)]

    def grid_links(self, r1, r2=None):
        """{dòng: (link OG, link NORMAL)} – đọc cả link dán thẳng, =HYPERLINK và link chèn (Ctrl+K)."""
        cols = [self.cols["og"]] + ([self.cols["norm"]] if "norm" in self.cols else [])
        ranges = [self.q + f"{col_letter(c)}{r1}:{col_letter(c)}{r2 or ''}" for c in cols]
        meta = api(self.ss.fetch_sheet_metadata, params={
            "ranges": ranges,
            "fields": "sheets(data(startRow,rowData(values(hyperlink,formattedValue,"
                      "userEnteredValue(formulaValue),textFormatRuns(format(link(uri)))))))"})
        out = {}
        try:
            datas = meta["sheets"][0].get("data", [])
        except Exception:
            datas = []
        for ci, d in enumerate(datas[:len(cols)]):
            start = d.get("startRow", 0) + 1
            for k, rd in enumerate(d.get("rowData", []) or []):
                v = (rd.get("values") or [{}])[0]
                u = v.get("hyperlink") or ""
                if not u:
                    u = next((x["format"]["link"]["uri"] for x in v.get("textFormatRuns", []) or []
                              if x.get("format", {}).get("link", {}).get("uri")), "")
                if not u:
                    u = link_from((v.get("userEnteredValue") or {}).get("formulaValue", "")) or link_from(v.get("formattedValue", ""))
                u = norm_url(u) if u.startswith("http") else ""
                r = start + k
                og, nm = out.get(r, ("", ""))
                out[r] = (u, nm) if ci == 0 else (og, u)
        return out

    def thumb_links(self, row):
        """Link BẠN dán ở cột ảnh Thumbnail (bỏ phần '🔗 Nguồn:' do tool tự ghi)."""
        if "thumb" not in self.cols:
            return []
        c = col_letter(self.cols["thumb"])
        meta = api(self.ss.fetch_sheet_metadata, params={
            "ranges": [self.q + f"{c}{row}:{c}{row}"],
            "fields": "sheets(data(rowData(values(hyperlink,formattedValue,textFormatRuns(startIndex,format(link(uri)))))))"})
        try:
            v = meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]
        except Exception:
            return []
        text = v.get("formattedValue", "") or ""
        cut = text.find(LINK_MARK)
        user_text = text if cut < 0 else text[:cut]
        cut16 = utf16_len(user_text)
        urls = re.findall(r"https?://[^\s\"'<>]+", user_text)
        for x in v.get("textFormatRuns", []) or []:
            u = ((x.get("format") or {}).get("link") or {}).get("uri")
            if u and x.get("startIndex", 0) < cut16:
                urls.append(u)
        if v.get("hyperlink") and cut < 0:
            urls.append(v["hyperlink"])
        out = []
        for u in urls:
            u = norm_url(u.rstrip(").,;"))
            if u.startswith("http") and u not in out:
                out.append(u)
        return out[:3]

    def row_links(self, row):
        """(og, norm) của 1 dòng – nhận mọi link http(s): Yupoo, trang hãng, shop…"""
        return self.grid_links(row, row).get(row, ("", ""))

    def locate(self, row, url):
        """Tìm lại dòng theo link (phòng khi bạn sort / chèn / xoá dòng trong lúc chạy)."""
        og, nm = self.row_links(row)
        if (og or nm) == url:
            return row
        best = 0
        for r, (o, n) in self.grid_links(HEADER_ROW + 1).items():
            if (o or n) == url and (not best or abs(r - row) < abs(best - row)):
                best = r
        return best

    def row_values(self, row):
        last = max(self.cols.values())
        vals = self.batch_get([f"A{row}:{col_letter(last)}{row}"])[0]
        vals = vals[0] if vals else []
        return lambda key: (vals[self.cols[key] - 1].strip() if key in self.cols and self.cols[key] - 1 < len(vals) else "")

    def hidden_rows(self, rows):
        """Các dòng đang bị bộ lọc ẩn (1 lần gọi API cho cả nhóm)."""
        if not rows:
            return set()
        r1, r2 = min(rows), max(rows)
        meta = api(self.ss.fetch_sheet_metadata, params={
            "ranges": self.q + f"A{r1}:A{r2}",
            "fields": "sheets(data(rowMetadata(hiddenByFilter,hiddenByUser)))"})
        out = set()
        try:
            rm = meta["sheets"][0]["data"][0].get("rowMetadata", [])
            for i, m in enumerate(rm):
                if m.get("hiddenByFilter") or m.get("hiddenByUser"):
                    out.add(r1 + i)
        except Exception:
            pass
        return out & set(rows)

    def clear_status(self, rows):
        self.apply([self.cell_req(r, self.cols["status"], text="", bg="#ffffff") for r in rows])

    def password(self, row):
        v = self.batch_get([f"{PASSWORD_COL}{row}"])[0]
        return (v[0][0] if v and v[0] else "").strip()

    # ---- ghi ----
    def cell_req(self, row, col, text=None, bg=None, links=None, note=None):
        cell, fields = {}, []
        if text is not None:
            cell["userEnteredValue"] = {"stringValue": text}
            fields.append("userEnteredValue")
            if links is not None:
                cell["textFormatRuns"] = links
                fields.append("textFormatRuns")
        if bg is not None:
            cell["userEnteredFormat"] = {"backgroundColor": hex_rgb(bg)}
            fields.append("userEnteredFormat.backgroundColor")
        if note is not None:
            cell["note"] = note
            fields.append("note")
        return {"updateCells": {
            "range": {"sheetId": self.gid, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": col - 1, "endColumnIndex": col},
            "rows": [{"values": [cell]}], "fields": ",".join(fields)}}

    def apply(self, reqs):
        if reqs:
            api(self.ss.batch_update, {"requests": reqs})

    def set_status(self, row, text):
        icon = next((k for k in COLORS if text.startswith(k)), None)
        bg = COLORS["check"] if text.startswith("✅") and "⚠" in text else COLORS.get(icon)
        self.apply([self.cell_req(row, self.cols["status"], text=text + time.strftime("  (%H:%M)"), bg=bg)])
        if text.startswith("✅"):
            self.recent_done = [r for r in self.recent_done if r != row][-30:] + [row]
        st = self.job_state(text)
        j = self.jobs.setdefault(row, {})
        j.update({"st": st, "t": text[:100], "ts": time.time()})
        self.save_jobs()

    # ---- yêu cầu từ menu + tiến độ (dữ liệu ẩn của file, không đụng ô) ----
    def read_requests(self):
        meta = api(self.ss.fetch_sheet_metadata, params={"fields": "developerMetadata(metadataId,metadataKey,metadataValue)"})
        out = []
        for m in meta.get("developerMetadata", []) or []:
            if m.get("metadataKey") != "AIQ_REQ":
                continue
            try:
                v = json.loads(m.get("metadataValue") or "{}")
            except Exception:
                v = {}
            v["_id"] = m.get("metadataId")
            out.append(v)
        return sorted(out, key=lambda x: x.get("t", 0))

    def delete_meta_ids(self, ids):
        ids = [i for i in ids if i is not None]
        if ids:
            api(self.ss.batch_update, {"requests": [{"deleteDeveloperMetadata": {"dataFilter": {
                "developerMetadataLookup": {"metadataId": i}}}} for i in ids]})

    def put_meta(self, key, value):
        api(self.ss.batch_update, {"requests": [
            {"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {"metadataKey": key}}}},
            {"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": key, "metadataValue": value,
                "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}}]})

    def handle_request(self, req, last_try):
        """Menu gửi: run / retry / cancel / clear cho vùng dòng đã chọn."""
        a = req.get("a")
        if int(req.get("s", -1)) != int(self.gid):
            log(f"   (bỏ yêu cầu ở tab '{req.get('n')}' – tool đang chạy tab '{self.ws.title}')")
            return f"yêu cầu ở tab {req.get('n')} bị bỏ qua – tool chạy tab {self.ws.title}"
        rows = sorted({r for x in req.get("r", []) for r in range(max(int(x[0]), HEADER_ROW + 1), int(x[1]) + 1)})
        if not rows:
            return ""
        st_map = self.status_range(rows[0], rows[-1])     # chỉ đọc vùng đã chọn
        links = self.grid_links(rows[0], rows[-1])
        last_data = max([r for r, (o, n) in links.items() if o or n] + [r for r, v in st_map.items() if v] + [0])
        rows = [r for r in rows if r <= last_data]
        hidden = self.hidden_rows(rows) if rows else set()
        reqs, n_ok, n_skip = [], 0, 0
        for r in rows:
            if r in hidden:
                continue
            cur = st_map.get(r, "")
            icon = next((k for k in ("⏳", "🔄", "👉", "✅", "⚠") if cur.startswith(k)), "")
            text = None
            if a == "run":
                mine = self.jobs.get(r, {}).get("st") in ("pending", "running")
                if icon in ("⏳", "🔄", "👉") and mine:
                    n_skip += 1          # đang chờ / đang chạy thật
                    continue
                if not any(links.get(r, ("", ""))):
                    n_skip += 1
                    continue
                text = "⏳ Chờ" + time.strftime("  (%H:%M)")
            elif a == "retry" and icon == "⚠":
                text = "⏳ Chờ – chạy lại" + time.strftime("  (%H:%M)")
            elif a == "cancel" and icon == "⏳":
                text = ""
            elif a == "clear" and icon and icon not in ("🔄", "👉"):
                text = ""
            if text is None:
                continue
            bg = COLORS["⏳"] if text else "#ffffff"
            reqs.append(self.cell_req(r, self.cols["status"], text=text, bg=bg))
            last_try.pop(r, None)
            if text:
                o, nm = links.get(r, ("", ""))
                self.jobs[r] = {"st": "pending", "t": text, "url": o or nm, "ts": time.time()}
            else:
                self.jobs.pop(r, None)
            n_ok += 1
        for k in range(0, len(reqs), 400):
            self.apply(reqs[k:k + 400])
        self.save_jobs()
        name = {"run": "khoá", "retry": "chạy lại", "cancel": "huỷ", "clear": "xoá trạng thái"}.get(a, a)
        log(f"📨 Yêu cầu {name}: {n_ok} dòng" + (f", bỏ qua {n_skip} (đang chờ/chạy hoặc không có link)" if n_skip else "")
            + (f", {len(hidden)} dòng bị lọc ẩn" if hidden else ""))
        return ""

    def publish_progress(self, note="", force=False):
        """Gửi tiến độ (từ danh sách việc trên máy, không đọc sheet) lên bảng tiến độ."""
        icon = {"pending": "⏳", "running": "🔄", "done": "✅", "check": "✅", "error": "⚠"}
        c = {"⏳": 0, "🔄": 0, "👉": 0, "✅": 0, "⚠": 0, "check": 0}
        active = []
        for r in sorted(self.jobs):
            j = self.jobs[r]
            st = j.get("st")
            if st not in icon:
                continue
            k = "👉" if j.get("t", "").startswith("👉") else icon[st]
            c[k] += 1
            if st == "check":
                c["check"] += 1
            if st in ("pending", "running", "error"):
                active.append({"r": r, "t": j.get("t", "")[:100]})
        done = [{"r": r, "t": self.jobs[r].get("t", "")[:100]} for r in reversed(self.recent_done)
                if self.jobs.get(r, {}).get("st") in ("done", "check")][:10]
        body = {"sheet": self.ws.title, "c": c, "items": active[:30] + done, "note": note}
        key = json.dumps(body, ensure_ascii=False, sort_keys=True)
        now = time.time()
        if not force and ((key == self.last_prog and now - self.last_pub < HEARTBEAT_SEC) or now - self.last_pub < 4):
            return
        body["hb"] = int(now * 1000)
        try:
            self.put_meta("AIQ_PROGRESS", json.dumps(body, ensure_ascii=False))
            self.last_prog, self.last_pub = key, now
        except Exception as e:
            if not getattr(self, "_pub_warned", False):
                log("   (không gửi được tiến độ lên bảng:", str(e)[:120], ")")
                self._pub_warned = True

    def heartbeat(self):
        """Báo 'Python đang chạy' qua dữ liệu ẩn của file (không ghi vào ô nào → không vướng vùng bị khoá)."""
        if time.time() - self.last_hb < HEARTBEAT_SEC:
            return
        self.last_hb = time.time()
        key = "AIQ_PY_HEARTBEAT"
        try:
            api(self.ss.batch_update, {"requests": [
                {"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {"metadataKey": key}}}},
                {"createDeveloperMetadata": {"developerMetadata": {
                    "metadataKey": key, "metadataValue": str(int(time.time() * 1000)),
                    "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}},
            ]})
        except Exception as e:
            if not getattr(self, "_hb_warned", False):
                log("   (không ghi được nhịp tim – bảng tiến độ có thể báo 🔴, không ảnh hưởng việc chạy):", str(e)[:120])
                self._hb_warned = True


# ======================= YUPOO TRÊN CHROME =======================
JS_READY = r"""() => {
  if (document.querySelector('input[type=password]')) return false;
  return [...document.querySelectorAll('img')].some(i => {
    const u = (i.getAttribute('data-origin-src') || '') + (i.getAttribute('data-src') || '') + (i.getAttribute('src') || '');
    return /photo\.yupoo\.com/.test(u) && !/logo|avatar/i.test(u);
  });
}"""

JS_BROKEN = r"""() => {
  if (document.querySelector('input[type=password]')) return false;
  const t = (document.body ? document.body.innerText : '').slice(0, 3000);
  return /(不存在|已删除|已被删除|does not exist|has been deleted|not found|404)/i.test(t);
}"""

JS_EXTRACT = r"""() => {
  const t = s => ((document.querySelector(s) || {}).innerText || '').trim();
  const title = t('.showalbumheader__gallerytitle') || document.title;
  const desc = t('.showalbumheader__gallerysubtitle');
  const urls = [];
  document.querySelectorAll('img').forEach(i => {
    for (const a of ['data-origin-src', 'data-src', 'src']) {
      let u = i.getAttribute(a) || '';
      if (/photo\.yupoo\.com/.test(u)) { if (u.startsWith('//')) u = 'https:' + u; urls.push(u); break; }
    }
  });
  return { title, desc, urls };
}"""


JS_GENERIC = r"""() => {
  const meta = n => { const m = document.querySelector(`meta[property="${n}"],meta[name="${n}"]`); return m ? (m.content || '').trim() : ''; };
  const title = meta('og:title') || document.title || '';
  const body = (document.body ? document.body.innerText : '').replace(/\s+\n/g, '\n').slice(0, 2500);
  const desc = [meta('og:description') || meta('description'), body].filter(Boolean).join('\n');
  const imgs = [];
  const og = meta('og:image');
  if (og) imgs.push({ u: og, a: 1e9 });
  document.querySelectorAll('img').forEach(i => {
    const u = i.currentSrc || i.src || i.getAttribute('data-src') || '';
    const a = (i.naturalWidth || 0) * (i.naturalHeight || 0);
    if (u && /^https?:/.test(u) && a >= 250 * 250 && !/logo|icon|sprite|avatar/i.test(u)) imgs.push({ u, a });
  });
  imgs.sort((x, y) => y.a - x.a);
  const seen = new Set(), urls = [];
  imgs.forEach(x => { if (!seen.has(x.u)) { seen.add(x.u); urls.push(x.u); } });
  return { title, desc, urls: urls.slice(0, 8) };
}"""

JS_GENERIC_READY = r"""() => document.readyState === 'complete' && (document.body ? document.body.innerText.length : 0) > 300"""


def open_generic(page, url, on_wait, wait=WAIT_VERIFY_SEC):
    """Trang thường (trang hãng, shop…): True | "BROKEN" | False."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("   Không mở được link:", str(e)[:100])
        return "BROKEN"
    if resp is not None and resp.status in (404, 410):
        return "BROKEN"
    warned, t0 = False, time.time()
    while time.time() - t0 < wait:
        try:
            if page.evaluate(JS_GENERIC_READY):
                page.wait_for_timeout(2500)   # chờ ảnh tải
                return True
        except Exception:
            pass
        if not warned and time.time() - t0 > 15:
            page.bring_to_front()
            log("   👉 Trang chưa hiện nội dung – có thể cần bạn xác thực trên Chrome")
            on_wait("trang yêu cầu xác minh")
            warned = True
        time.sleep(2)
    return False


def download_direct(context, urls, n=MAX_IMAGES):
    out = []
    for u in urls:
        try:
            r = context.request.get(u, timeout=30000)
            ctype = r.headers.get("content-type", "")
            body = r.body()
            if r.ok and ctype.startswith("image/") and 5000 < len(body) < 4_000_000 and "svg" not in ctype:
                out.append({"mime": ctype.split(";")[0], "data": base64.b64encode(body).decode()})
        except Exception:
            pass
        if len(out) >= n:
            break
    return out


def try_password(page, pw):
    box = page.locator("input[type=password]:visible").first
    box.fill(pw)
    box.press("Enter")
    time.sleep(3)
    if page.locator("input[type=password]:visible").count():
        for sel in ["button[type=submit]", "input[type=submit]", "form button", "button:has-text('确定')",
                    "button:has-text('OK')", "button:has-text('Submit')", "button:has-text('提交')"]:
            try:
                page.locator(sel).first.click(timeout=1500)
                time.sleep(3)
                break
            except Exception:
                pass


def open_album(page, url, pw, on_wait, wait=WAIT_VERIFY_SEC):
    """True = mở được | "BROKEN" = link hỏng | False = hết giờ chờ xác thực."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log("   Không mở được link:", str(e)[:100])
        return "BROKEN"
    if resp is not None and resp.status in (404, 410):
        return "BROKEN"
    tried_pw, warned, t0 = False, False, time.time()
    while time.time() - t0 < wait:
        try:
            if page.evaluate(JS_READY):
                return True
            if time.time() - t0 > 3 and page.evaluate(JS_BROKEN):
                return "BROKEN"
            if pw and not tried_pw and page.locator("input[type=password]:visible").count():
                log("   🔑 Điền mật khẩu")
                try_password(page, pw)
                tried_pw = True
                continue
        except Exception:
            pass
        if not warned:
            page.bring_to_front()
            has_pw_box = page.locator("input[type=password]").count() > 0
            reason = "mật khẩu sai / thiếu ở cột " + PASSWORD_COL if (tried_pw or (has_pw_box and not pw)) else "captcha / xác minh"
            log("   👉 CẦN BẠN XÁC THỰC trên Chrome:", reason)
            on_wait(reason)
            warned = True
        time.sleep(2)
    return False


def pick_images(urls):
    seen, bases = set(), []
    for u in urls:
        if re.search(r"logo|avatar", u, re.I):
            continue
        b = u.rsplit("/", 1)[0]
        if b not in seen:
            seen.add(b)
            bases.append(b)
    if len(bases) <= MAX_IMAGES:
        return bases
    return bases[:3] + bases[-(MAX_IMAGES - 3):]   # 3 ảnh đầu + ảnh cuối (bảng size, tem hộp)


def download_images(context, host, bases):
    out = []
    for b in bases:
        try:
            r = context.request.get(b + "/medium.jpg", headers={"Referer": f"https://{host}/"}, timeout=30000)
            ctype = r.headers.get("content-type", "")
            body = r.body()
            if r.ok and ctype.startswith("image/") and len(body) < 4_000_000:
                out.append({"mime": ctype.split(";")[0], "data": base64.b64encode(body).decode()})
        except Exception:
            pass
    return out


# ======================= GEMINI =======================
class GeminiBusy(Exception):
    pass


PROMPT = """You are a sneaker/fashion product researcher. Identify the EXACT product shown in this Yupoo supplier album, then use Google Search to verify official details.

Page / album title: {title}
Page / album text: {desc}
Brand abbreviation used in our sheet: {brand}
Model key in our sheet: {model}
Already known nickname: {nick}
Already known style code: {style}

Rules:
- Style codes in supplier titles are often correct — verify them via search.
- If RETAIL / OFFICIAL PRODUCT PAGE text is provided, it is THIS exact product: take style code, colorway, release date from it first.
  Use your own search only for fields that the provided pages do not give.
- women_size / men_size: take ONLY from the page/album title, text or images (size charts, text like "36-45"). Women usually up to 40, men from 40. Format "36-40". Leave "" if not shown.
- nickname format: "<Brand Abbr> <Model> '<Colorway nickname>'" e.g. "AMR Bone Runner 'Black White'" (use the brand abbreviation, not the full brand name).
- colorway: official colorway with slashes, e.g. "Red/White".
- COLOURS MUST COME FROM THE PHOTOS: the photos show ONE product (first photos = the shoe, last photos may be box/label/size chart).
  The colorway and the colour words in the nickname must match colours clearly visible on THIS shoe.
  Never add a colour you cannot see on the shoe. Search results often mention OTHER colorways of the same model – ignore those colours.
  Use search only to confirm style code / release date / official colour names of THIS colorway.
- release_date: format "MMMM dd,yyyy" e.g. "March 29,2022". "" if unknown.
- Never invent data. If unsure about a field, return "".
- sources: real web pages you used (brand site, StockX, GOAT, KicksOnFire, Sneaker News, Farfetch…). No Yupoo links.

Reply with ONLY a JSON object:
{{"product_name":"","nickname":"","style_code":"","colorway":"","release_date":"","women_size":"","men_size":"","confidence":"high|medium|low","sources":[""]}}"""


def resolve(u):
    if "grounding-api-redirect" not in u:
        return u
    try:
        r = requests.get(u, allow_redirects=False, timeout=15)
        return r.headers.get("Location") or u
    except Exception:
        return u


def brand_abbr(known):
    """Cột Brand Abbr trống → lấy chữ viết tắt hãng ở đầu cột Model Key (vd 'AMR Bone Runner' → 'AMR')."""
    b = known("brand")
    if b:
        return b
    m = re.match(r"\s*([A-Z0-9&]{2,5})\b", known("model") or "")
    if m and m.group(1) not in ("OG", "NEW", "TOP", "HOT", "SALE", "ONLY", "ALL", "REP"):
        return m.group(1)
    return ""


def build_prompt(album, known):
    return PROMPT.format(title=album["title"] or "(none)", desc=album["desc"] or "(none)",
                         brand=brand_abbr(known) or "(unknown – infer the usual short abbreviation, e.g. AMR for Amiri)", model=known("model") or "(unknown)",
                         nick=known("nickname") or "(none)", style=known("style") or "(none)")


def parse_json(text):
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise RuntimeError("AI không trả JSON")
    return json.loads(m.group(0))


def merge_links(found, data):
    links = []
    for u in list(found) + list(data.get("sources") or []):
        u = str(u or "").strip()
        if u.startswith("http") and "yupoo.com" not in u and u not in links and len(links) < 4:
            links.append(u)
    return links


def ask_ai(album, known):
    """Thử lần lượt: Gemini → ChatGPT → Claude (bỏ qua AI chưa có key). Trả (data, links, tên AI)."""
    prompt = build_prompt(album, known)
    chain = [("Gemini", "gemini_api_key", ask_gemini),               # miễn phí, tự search Google
             ("OpenRouter", "openrouter_api_key", ask_openrouter),   # miễn phí + search DuckDuckGo
             ("Groq", "groq_api_key", ask_groq),                     # miễn phí + search DuckDuckGo
             ("ChatGPT", "openai_api_key", ask_openai),              # trả phí
             ("Claude", "claude_api_key", ask_claude)]               # trả phí
    chain = [c for c in chain if CFG.get(c[1])]
    if not chain:
        raise RuntimeError("Chưa có API key AI nào – chạy: python yupoo_ai_runner.py --setup")
    errors, all_busy, day = [], True, False
    for name, _, fn in chain:
        try:
            data, links = fn(prompt, album)
            return data, links, name
        except QuotaDay as e:
            errors.append(f"{name}: hết lượt hôm nay")
            day = True
        except GeminiBusy as e:
            errors.append(f"{name}: hết lượt/quá tải")
        except Exception as e:
            all_busy = False
            errors.append(f"{name}: {str(e)[:60]}")
        log(f"   ↪ {errors[-1]} → thử AI tiếp theo")
    if all_busy and day:
        raise QuotaDay("Tất cả AI đều hết lượt (" + "; ".join(errors) + ")")
    if all_busy:
        raise GeminiBusy("Tất cả AI đều hết lượt/quá tải (" + "; ".join(errors) + ")")
    raise RuntimeError("Tất cả AI đều lỗi: " + "; ".join(errors))


# ---------- AI miễn phí không tự search được: tự search DuckDuckGo giúp ----------
IDENTIFY_PROMPT = """Look at these product photos from a Chinese supplier album.
Album title: {title}
Album description: {desc}
Brand abbreviation in our sheet: {brand}
Identify the product as precisely as possible (brand, model, colorway, style code if visible on box labels).
Reply ONLY JSON: {{"product_name":"", "style_code_guess":"", "search_query":"<best English web search query to find this exact product>"}}"""


def web_search(query, n=6):
    if not DDGS or not query:
        return []
    try:
        with DDGS() as d:
            res = list(d.text(query, max_results=n))
        return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body", "")}
                for r in res if (r.get("href") or r.get("url"))]
    except Exception as e:
        log("   (search DuckDuckGo lỗi:", str(e)[:80], ")")
        return []


def chat_call(url, key, model, text, images, extra_headers=None):
    """Gọi API kiểu OpenAI chat. Trả (text, đã_xem_ảnh). Model không nhận ảnh → gửi chỉ chữ (đánh dấu không xem ảnh)."""
    for with_img in ([True, False] if images else [False]):
        content = [{"type": "text", "text": text}]
        if with_img:
            for im in images:
                content.append({"type": "image_url", "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}})
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        h.update(extra_headers or {})
        r = requests.post(url, headers=h, timeout=180, data=json.dumps({
            "model": model, "temperature": 0.2,
            "messages": [{"role": "user", "content": content}]}))
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code == 429 or r.status_code >= 500:
            raise GeminiBusy(f"HTTP {r.status_code}")
        if r.status_code == 200:
            try:
                return (j["choices"][0]["message"]["content"] or ""), with_img
            except Exception:
                raise RuntimeError("trả lời rỗng")
        msg = str((j.get("error") or {}).get("message", r.status_code))
        if with_img and re.search(r"image|vision|multimodal|content", msg, re.I):
            log("   (model không xem được ảnh → gửi chỉ chữ)")
            continue
        raise RuntimeError(msg[:150])
    raise RuntimeError("không gọi được")


VERIFY_PROMPT = """Look ONLY at the shoe in these photos (ignore boxes, labels, size charts).
Proposed colorway: "{colorway}"
Proposed nickname: "{nickname}"
1) List the main colours you can clearly see on the shoe itself.
2) Is every colour in the proposed colorway really visible on the shoe? Any colour that is NOT on the shoe makes it wrong.
Reply ONLY JSON: {{"colors_visible":[""], "colorway_ok":true, "wrong_colors":[""], "corrected_colorway":"", "corrected_nickname":""}}
corrected_* = fixed versions using only visible colours (same style/format as proposed), or "" if proposed is ok."""


def verify_colors(url, key, model, album, data, extra_headers=None):
    """Hỏi lại AI (kèm ảnh): màu đề xuất có đúng với đôi giày trong ảnh không → sửa nếu sai."""
    if not album["images"] or not (data.get("colorway") or data.get("nickname")):
        return data
    try:
        txt, saw = chat_call(url, key, model, VERIFY_PROMPT.format(
            colorway=data.get("colorway", ""), nickname=data.get("nickname", "")), album["images"], extra_headers)
        if not saw:
            return data
        v = parse_json(txt)
    except Exception as e:
        log("   (bỏ qua bước kiểm tra màu:", str(e)[:80], ")")
        return data
    if v.get("colorway_ok") is False or v.get("wrong_colors"):
        wrong = ", ".join(x for x in (v.get("wrong_colors") or []) if x)
        log(f"   🎨 Kiểm tra màu: SAI ({wrong or 'không khớp ảnh'}) – thấy: {', '.join(v.get('colors_visible') or [])}")
        if v.get("corrected_colorway"):
            data["colorway"] = v["corrected_colorway"]
        if v.get("corrected_nickname"):
            data["nickname"] = v["corrected_nickname"]
        data["confidence"] = "low"
        data["_note"] = "đã sửa màu theo ảnh"
    else:
        log("   🎨 Kiểm tra màu: khớp ảnh")
    return data


def ask_free_with_search(url, key, model, prompt, album, extra_headers=None):
    # Bước 1: nhìn ảnh + tiêu đề → đoán sản phẩm + câu search
    ident_txt, saw1 = chat_call(url, key, model, IDENTIFY_PROMPT.format(
        title=album["title"] or "(none)", desc=album["desc"][:1500] or "(none)", brand="(see photos)"),
        album["images"], extra_headers)
    try:
        ident = parse_json(ident_txt)
    except Exception:
        ident = {"search_query": album["title"]}
    q = ident.get("search_query") or " ".join(x for x in [ident.get("product_name"), ident.get("style_code_guess")] if x)
    results = web_search(q) + (web_search(ident["style_code_guess"] + " sneaker", 3) if ident.get("style_code_guess") else [])
    seen, uniq = set(), []
    for r in results:
        if r["url"] not in seen and "yupoo.com" not in r["url"]:
            seen.add(r["url"])
            uniq.append(r)
    log(f"   🔎 Search: {q[:70]} → {len(uniq)} kết quả")
    # Bước 2: đưa kết quả search vào để điền thông tin
    ctx = "\n".join(f"[{i + 1}] {r['title']} – {r['url']}\n    {r['snippet'][:300]}" for i, r in enumerate(uniq[:8]))
    full = (prompt + "\n\nYou cannot browse. Use these web search results ONLY to verify style code / release date "
            "(colours must still come from the photos; pick source URLs from this list):\n"
            + (ctx or "(no results – answer from images/title, confidence low)"))
    txt, saw2 = chat_call(url, key, model, full, album["images"], extra_headers)
    data = parse_json(txt)
    if album["images"] and not (saw1 or saw2):
        data["_blind"] = True            # AI không xem được ảnh → không tin màu
        data["confidence"] = "low"
    elif album["images"]:
        data = verify_colors(url, key, model, album, data, extra_headers)
    if not uniq and data.get("confidence") == "high":
        data["confidence"] = "medium"
    allowed = {r["url"] for r in uniq}
    srcs = [u for u in (data.get("sources") or []) if u in allowed] or [r["url"] for r in uniq[:3]]
    data["sources"] = srcs
    return data, merge_links([], data)


OR_STATE = {"models": [], "blocked": {}}


def openrouter_vision_models():
    """Model MIỄN PHÍ ($0) trên OpenRouter có nhận ảnh – tốt nhất trước."""
    if OR_STATE["models"]:
        return OR_STATE["models"]
    fixed = CFG.get("openrouter_model") or OPENROUTER_MODEL
    if fixed and fixed != "openrouter/free":
        OR_STATE["models"] = [fixed]
        return OR_STATE["models"]
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=60)
    cands = []
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        pr = m.get("pricing") or {}
        free = mid.endswith(":free") or (str(pr.get("prompt", "1")) in ("0", "0.0") and str(pr.get("completion", "1")) in ("0", "0.0"))
        mods = (m.get("architecture") or {}).get("input_modalities") or []
        if not free or "image" not in mods:
            continue
        score = 0
        for k, w in (("gemini", 6), ("qwen", 5), ("llama-4", 4), ("gemma", 3), ("mistral", 3), ("vl", 2)):
            if k in mid.lower():
                score = max(score, w)
        cands.append((score, m.get("context_length") or 0, mid))
    cands.sort(reverse=True)
    OR_STATE["models"] = [c[2] for c in cands] or ["openrouter/free"]
    log("   🤖 OpenRouter – model free xem được ảnh:", ", ".join(OR_STATE["models"][:5]))
    return OR_STATE["models"]


def ask_openrouter(prompt, album):
    last = None
    for mid in [m for m in openrouter_vision_models() if OR_STATE["blocked"].get(m, 0) < time.time()][:4]:
        try:
            data, links = ask_free_with_search("https://openrouter.ai/api/v1/chat/completions", CFG["openrouter_api_key"],
                                               mid, prompt, album, {"X-Title": "Yupoo AI Runner"})
            data["_model"] = mid
            return data, links
        except GeminiBusy as e:
            OR_STATE["blocked"][mid] = time.time() + 300
            last = e
        except Exception as e:
            OR_STATE["blocked"][mid] = time.time() + 600
            last = e
            log(f"   (OpenRouter {mid}: {str(e)[:80]} → thử model khác)")
    if isinstance(last, GeminiBusy) or last is None:
        raise GeminiBusy("OpenRouter hết lượt")
    raise last


def groq_model():
    if CFG.get("groq_model"):
        return CFG["groq_model"]
    r = requests.get("https://api.groq.com/openai/v1/models",
                     headers={"Authorization": "Bearer " + CFG["groq_api_key"]}, timeout=30)
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    pick = next((i for i in ids if re.search(r"vision|scout|maverick|llama-4", i, re.I)), None) \
        or next((i for i in ids if re.search(r"llama-3\.3-70b|70b", i)), None) or (ids[0] if ids else "")
    if not pick:
        raise RuntimeError("Groq không có model nào")
    CFG["groq_model"] = pick
    save_config()
    log("   🤖 Dùng model Groq:", pick)
    return pick


def ask_groq(prompt, album):
    return ask_free_with_search("https://api.groq.com/openai/v1/chat/completions", CFG["groq_api_key"],
                                groq_model(), prompt, album)


def ask_claude(prompt, album):
    content = []
    for im in album["images"]:
        content.append({"type": "image", "source": {"type": "base64", "media_type": im["mime"], "data": im["data"]}})
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]
    text, found = "", []
    for _ in range(3):  # "pause_turn" = Claude còn đang tìm, gửi tiếp
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CFG["claude_api_key"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            data=json.dumps({"model": CFG.get("claude_model") or CLAUDE_MODEL, "max_tokens": 2048,
                             "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                             "messages": messages}),
            timeout=240)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code in (429, 529) or r.status_code >= 500:
            raise GeminiBusy(f"Claude quá tải (HTTP {r.status_code})")
        if r.status_code != 200:
            raise RuntimeError("Claude: " + str((j.get("error") or {}).get("message", r.status_code)))
        for b in j.get("content", []):
            if b.get("type") == "text":
                text += b.get("text", "")
                for c in b.get("citations") or []:
                    if c.get("url"):
                        found.append(c["url"])
            elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                for res in b["content"]:
                    if res.get("url"):
                        found.append(res["url"])
        if j.get("stop_reason") != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": j.get("content", [])}]
    data = parse_json(text)
    return data, merge_links(found, data)


def ask_openai(prompt, album):
    content = [{"type": "input_text", "text": prompt}]
    for im in album["images"]:
        content.append({"type": "input_image", "image_url": f"data:{im['mime']};base64,{im['data']}"})
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer " + CFG["openai_api_key"], "Content-Type": "application/json"},
        data=json.dumps({"model": CFG.get("openai_model") or OPENAI_MODEL,
                         "tools": [{"type": "web_search"}],
                         "include": ["web_search_call.action.sources"],
                         "input": [{"role": "user", "content": content}]}),
        timeout=240)
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code == 429 or r.status_code >= 500:
        raise GeminiBusy(f"Cả Gemini và ChatGPT đều hết lượt/quá tải (ChatGPT HTTP {r.status_code})")
    if r.status_code != 200:
        raise RuntimeError("ChatGPT: " + str((j.get("error") or {}).get("message", r.status_code)))
    text, found = "", []
    for item in j.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text += c.get("text", "")
                    for a in c.get("annotations", []) or []:
                        if a.get("url"):
                            found.append(a["url"])
        elif item.get("type") == "web_search_call":
            for src in ((item.get("action") or {}).get("sources") or []):
                if src.get("url"):
                    found.append(src["url"])
    data = parse_json(text)
    return data, merge_links(found, data)


def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in CFG.items()}, f, indent=2, ensure_ascii=False)


def discover_gemini_models():
    """Danh sách model Gemini Flash có generateContent, tốt nhất trước, bản Lite cuối."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"pageSize": 200}, headers={"x-goog-api-key": CFG["gemini_api_key"]}, timeout=60)
    cands = []
    for m in r.json().get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []) or "flash" not in name:
            continue
        if re.search(r"tts|image|live|audio|embed|thinking|learnlm|8b", name):
            continue
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        v = float(ver.group(1)) if ver else 0
        lite = 1 if "lite" in name else 0
        stable = 0 if re.search(r"preview|exp|\d{2}-\d{2}", name) else 1
        cands.append((-lite, stable, v, -len(name), name))
    cands.sort(reverse=True)
    names = [c[-1] for c in cands]
    if not names:
        raise RuntimeError("Không tìm được model Gemini Flash nào cho API key này")
    return names


GEMINI_STATE = {"models": [], "blocked": {}, "no_search_until": 0}   # blocked: model -> hết hạn chặn (epoch)


def gemini_candidates():
    if not GEMINI_STATE["models"]:
        GEMINI_STATE["models"] = discover_gemini_models()
        log("   🤖 Model Gemini có thể dùng:", ", ".join(GEMINI_STATE["models"][:6]))
    now = time.time()
    order = GEMINI_STATE["models"]
    if CFG.get("gemini_model") in order:   # model chạy được lần trước lên đầu
        order = [CFG["gemini_model"]] + [m for m in order if m != CFG["gemini_model"]]
    return [m for m in order if GEMINI_STATE["blocked"].get(m, 0) < now]


class QuotaDay(Exception):
    """Hết lượt miễn phí cả ngày."""


def gemini_call(model, prompt, album, use_search):
    parts = [{"text": prompt if use_search else prompt + "\n(Web search is unavailable now: answer from the images and title only, set confidence to low or medium, sources [])"}]
    for im in album["images"]:
        parts.append({"inline_data": {"mime_type": im["mime"], "data": im["data"]}})
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.2}}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": CFG["gemini_api_key"], "Content-Type": "application/json"},
                      data=json.dumps(body), timeout=180)
    try:
        j = r.json()
    except Exception:
        j = {}
    return r.status_code, j, str((j.get("error") or {}).get("message", ""))


def ask_gemini(prompt, album):
    """Thử lần lượt các model Gemini; hết lượt model này thì sang model khác; search hết lượt thì bỏ search."""
    day_limited = False
    for use_search in (True, False):
        if use_search and GEMINI_STATE["no_search_until"] > time.time():
            continue
        search_quota = False
        for model in gemini_candidates():
            code, j, msg = gemini_call(model, prompt, album, use_search)
            if code == 200:
                if CFG.get("gemini_model") != model:
                    CFG["gemini_model"] = model
                    save_config()
                    log("   🤖 Dùng model Gemini:", model)
                cand = (j.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
                data = parse_json(text)
                if not use_search:
                    data["confidence"] = "low" if data.get("confidence") == "low" else "medium"
                found = [resolve(c["web"].get("uri", "")) for c in
                         (cand.get("groundingMetadata") or {}).get("groundingChunks", []) if c.get("web")]
                return data, merge_links(found, data)
            log(f"   Gemini [{model}{'' if use_search else ', không search'}] HTTP {code}: {msg[:220]}")
            if code == 429 or code >= 500:
                per_day = bool(re.search(r"per ?day|PerDay|daily", msg, re.I))
                day_limited = day_limited or per_day
                if use_search and re.search(r"search|grounding", msg, re.I):
                    search_quota = True
                    break                      # hết lượt search → thử lại không search
                GEMINI_STATE["blocked"][model] = time.time() + (6 * 3600 if (per_day or "limit: 0" in msg) else 90)
                continue
            if code in (400, 403, 404) and re.search(r"no longer available|not found|not supported|deprecat", msg, re.I):
                GEMINI_STATE["blocked"][model] = time.time() + 7 * 86400
                continue
            if use_search and re.search(r"search|grounding|tool", msg, re.I):
                search_quota = True
                break
            raise RuntimeError("Gemini: " + msg[:150])
        if use_search and search_quota:
            GEMINI_STATE["no_search_until"] = time.time() + 3600
            log("   ↪ Hết lượt search miễn phí → nhận diện bằng ảnh + tiêu đề (không search) trong 1 giờ")
    if day_limited:
        raise QuotaDay("Gemini hết lượt miễn phí hôm nay")
    raise GeminiBusy("Gemini hết lượt/quá tải ở mọi model")


# ======================= XỬ LÝ 1 DÒNG =======================
def thumb_text(existing, links, confidence, product, ai_name=""):
    base = existing.split(LINK_MARK)[0].rstrip()
    text = (base + "\n" if base else "") + LINK_MARK + (f" [{ai_name}]" if ai_name else "")
    if product:
        text += " " + product
    if confidence and confidence != "high":
        text += f" (⚠️ độ tin cậy: {confidence} – kiểm tra lại)"
    runs = []
    if not links:
        text += "\n(không tìm thấy nguồn)"
    for u in links:
        text += "\n"
        start = utf16_len(text)
        text += u
        runs.append({"startIndex": start, "format": {"link": {"uri": u}}})
        runs.append({"startIndex": utf16_len(text), "format": {}})
    if runs and runs[-1]["startIndex"] >= utf16_len(text):
        runs.pop()
    if runs and runs[0]["startIndex"] > 0:
        runs.insert(0, {"startIndex": 0, "format": {}})
    return text, runs


ALBUM_CACHE = {}   # url -> album đã lấy (để thử lại AI không phải mở Chrome lại)


def process_row(sh, context, page, row):
    og, nm = sh.row_links(row)
    url = og or nm
    want = (sh.jobs.get(row) or {}).get("url", "")
    if want and url != want:                     # dòng đã bị dời (sort/chèn/xoá) → tìm lại theo link
        new = sh.locate(row, want)
        if not new:
            log(f"   ❌ Dòng {row}: không còn thấy link đã khoá – bỏ qua")
            sh.jobs.pop(row, None)
            sh.save_jobs()
            return
        sh.jobs[new] = sh.jobs.pop(row)
        row = new
        og, nm = sh.row_links(row)
        url = og or nm
    if not url:
        sh.set_status(row, "⚠️ Lỗi – không có link ở cột OG / NORMAL")
        return
    alt = nm if (og and nm and nm != og) else ""
    try:
        extra_links = [u for u in sh.thumb_links(row) if u not in (og, nm)]
    except Exception:
        extra_links = []
    if extra_links:
        log(f"   🔗 Thêm {len(extra_links)} link từ cột ảnh Thumbnail")
    pw = sh.password(row)
    log(f"▶ Dòng {row}: {url}")
    if url in ALBUM_CACHE:
        log("   (dùng lại ảnh đã lấy, không mở Chrome)")
        return ask_and_write(sh, row, url, ALBUM_CACHE[url])
    sh.set_status(row, "🔄 Đang mở Yupoo" + (" + điền mật khẩu" if pw else ""))

    def on_wait(reason):
        r = sh.locate(row, url) or row
        sh.set_status(r, "👉 CHỜ BẠN XÁC THỰC TRÊN CHROME – " + reason)

    ok = False
    for n, u in enumerate([url] + ([alt] if alt else [])):
        if n:
            log("   ↪ Link OG hỏng → thử link NORMAL:", u)
            sh.set_status(row, "🔄 Link OG hỏng → thử link NORMAL")
        res = open_album(page, u, pw, on_wait) if "yupoo.com" in u.lower() else open_generic(page, u, on_wait)
        if res is True:
            ok = True
            break
        if res is False:
            sh.set_status(sh.locate(row, url) or row, "⚠️ Lỗi – hết thời gian chờ xác thực")
            return
        log("   ⚠️ Link hỏng:", u)
    if not ok:
        sh.set_status(row, "⚠️ Lỗi – " + ("cả link OG và NORMAL đều hỏng" if alt else "link Yupoo hỏng / album không tồn tại"))
        return

    info, images, page_src = grab(page, context)
    log(f"   📷 {len(images)} ảnh | {info['title'][:80]}")
    if not images and not info["title"]:
        sh.set_status(row, "⚠️ Lỗi – Chrome không lấy được ảnh/tiêu đề")
        return
    album = {"title": info["title"], "desc": info["desc"], "images": images, "page": page_src,
             "has_yupoo": not page_src}

    # Mở thêm các link còn lại (NORMAL + link bạn dán ở cột ảnh Thumbnail) → gom thêm thông tin
    others = []
    for u in ([alt] if (alt and n == 0) else []) + extra_links:
        if u and u not in others and u != url and u != page.url:
            others.append(u)
    album.setdefault("srcs", [])
    if page_src:
        album["srcs"].append(page_src)
    for k, other in enumerate(others, 1):
        log(f"   ➕ Mở thêm link {k + 1}:", other)
        sh.set_status(row, f"🔄 Đang đọc thêm link {k + 1}/{len(others) + 1}")
        p2 = context.new_page()
        try:
            r2 = (open_album(p2, other, pw, on_wait, wait=90) if "yupoo.com" in other.lower()
                  else open_generic(p2, other, on_wait, wait=90))
            if r2 is True:
                info2, images2, src2 = grab(p2, context)
                log(f"   📄 Link {k + 1}: {info2['title'][:80]} | {len(images2)} ảnh")
                merge_page(album, info2, images2, src2)
            else:
                log(f"   (không đọc được link {k + 1} – bỏ qua)")
        except Exception as e:
            log(f"   (lỗi khi đọc link {k + 1}:", str(e)[:80], ")")
        finally:
            try:
                p2.close()
            except Exception:
                pass
    if others:
        sh.set_status(row, f"🔄 Đã đọc {len(others) + 1} link")
    ALBUM_CACHE[url] = album
    return ask_and_write(sh, row, url, album)


def merge_page(album, info2, images2, src2):
    """Gộp thông tin 1 trang phụ vào album."""
    if src2:   # trang sản phẩm (StockX, GOAT, trang hãng…) = nguồn chuẩn
        block = (f"RETAIL / OFFICIAL PRODUCT PAGE for this exact product ({src2}):\n"
                 f"{info2['title']}\n{info2['desc'][:2000]}")
        album["desc"] = (block + "\n\n" + album["desc"])[:7000]
        album.setdefault("srcs", []).append(src2)
        if not album["images"]:
            album["images"] = images2[:MAX_IMAGES]
    else:      # Yupoo phụ → lấy ảnh (thấy màu + bảng size)
        album["desc"] = (album["desc"] + f"\n\n--- Supplier album ---\n{info2['title']}\n{info2['desc'][:1200]}")[:7000]
        if len(album["images"]) < MAX_IMAGES or not album.get("has_yupoo"):
            album["images"] = (images2 + album["images"])[:MAX_IMAGES]
        album["has_yupoo"] = True


def grab(page, context):
    """Lấy tiêu đề, chữ và ảnh của trang đang mở. Trả (info, images, nguồn_tham_khảo)."""
    page.wait_for_timeout(1500)
    if "yupoo.com" in page.url.lower():
        info = page.evaluate(JS_EXTRACT)
        host = re.match(r"https?://([^/?#]+)", page.url).group(1)
        return info, download_images(context, host, pick_images(info["urls"])), ""
    info = page.evaluate(JS_GENERIC)
    return info, download_direct(context, info["urls"]), page.url.split("#")[0]


def ask_and_write(sh, row, url, album):
    row = sh.locate(row, url) or row
    sh.set_status(row, "🔄 AI đang tìm thông tin")
    known = sh.row_values(row)
    data, links, ai_name = ask_ai(album, known)
    ALBUM_CACHE.pop(url, None)
    srcs = []
    for u in album.get("srcs", []) + ([album["page"]] if album.get("page") else []):
        if u and u not in srcs:
            srcs.append(u)
    if srcs:   # link trang sản phẩm bạn đưa đứng đầu, rồi tới link AI tìm thêm
        links = srcs + [l for l in links if l not in srcs][:max(1, 5 - len(srcs))]

    # Kiểm tra lại đúng dòng trước khi ghi (phòng sort / chèn / xoá dòng)
    row = sh.locate(row, url)
    if not row:
        log("   ❌ Dòng bị xoá/đổi link trong lúc chạy – không ghi")
        return
    cur = sh.row_values(row)
    low = data.get("confidence") and data.get("confidence") != "high"
    blind = data.get("_blind")
    reqs = []
    for key, field in [("nickname", "nickname"), ("style", "style_code"), ("colorway", "colorway"),
                       ("release", "release_date"), ("women", "women_size"), ("men", "men_size")]:
        v = str(data.get(field) or "").strip()
        if blind and key in ("nickname", "colorway"):
            continue                     # AI không xem được ảnh → không điền màu
        if key in sh.cols and v and (OVERWRITE or not cur(key)):
            reqs.append(sh.cell_req(row, sh.cols[key], text=v, bg=COLORS["check"] if low else None))
    if "thumb" in sh.cols:
        text, runs = thumb_text(cur("thumb"), links, data.get("confidence"), data.get("product_name"), ai_name)
        reqs.append(sh.cell_req(row, sh.cols["thumb"], text=text, links=runs))
    sh.apply(reqs)
    name = data.get("nickname") or data.get("product_name") or ""
    extra = " (AI không xem được ảnh – chưa điền màu)" if blind else (f" ({data['_note']})" if data.get("_note") else "")
    sh.set_status(row, ("✅ Xong – ⚠️ kiểm tra lại – " if low else "✅ Xong – ") + name + f" [{ai_name}]" + extra)
    log(f"   ✅ [{ai_name}] {name} | {data.get('style_code', '')} | tin cậy: {data.get('confidence')}")


# ======================= VÒNG CHÍNH =======================
def main():
    lock = single_instance()
    if not lock:
        log("Python đã đang chạy rồi – thoát bản trùng. (Muốn chạy lại: tắt bản cũ trước)")
        return
    load_config()
    log("🤖 Yupoo AI runner v7 – kết nối Google Sheet bằng service account…")
    try:
        sh = Sheet()
        sh.refresh_cols(force=True)
        log(f"✅ Kết nối OK – tab '{sh.ws.title}', cột AI STATUS = {col_letter(sh.cols['status'])}")
    except gspread.exceptions.APIError as e:
        log("❌ Google từ chối:", str(e)[:300])
        log(f"   → Bấm Chia sẻ sheet cho {SERVICE_EMAIL} quyền Người chỉnh sửa, và bật Google Sheets API.")
        return
    except Exception as e:
        log("❌ Không kết nối được Google Sheet:", e)
        return

    # Lần đầu chạy v7 (chưa có jobs.json): nhận hết dòng ⏳/🔄/👉 còn dở trên sheet (do bản cũ đánh dấu)
    if not os.path.exists(JOBS_FILE):
        try:
            log("   Lần đầu chạy v7 – đọc cột AI STATUS 1 lần để nhận các dòng đang chờ từ bản cũ…")
            n = 0
            for r, t in sh.status_column():
                if t.startswith("⏳") or t.startswith("🔄") or t.startswith("👉"):
                    sh.jobs[r] = {"st": "pending", "t": "⏳ Chờ", "url": "", "ts": time.time()}
                    n += 1
            sh.save_jobs()
            log(f"   → Nhận {n} dòng đang chờ")
        except Exception as e:
            log("   (chưa đọc được cột AI STATUS:", str(e)[:120], "– chọn lại các dòng ⏳ rồi bấm Update)")

    # Việc 🔄/👉 còn sót từ lần chạy trước (tắt máy giữa chừng) → đưa về ⏳
    try:
        for r, j in list(sh.jobs.items()):
            if j.get("st") == "running":
                sh.set_status(r, "⏳ Chờ – chạy lại")
        pend = sum(1 for j in sh.jobs.values() if j.get("st") == "pending")
        if pend:
            log(f"   Còn {pend} dòng chờ từ lần trước – chạy tiếp")
    except Exception as e:
        log("   (bỏ qua dọn trạng thái cũ:", str(e)[:120], ")")

    with sync_playwright() as p:
        context, last_work = None, time.time()
        last_try = {}          # dòng → lần thử gần nhất (dòng mới chưa thử được làm trước)
        ai_ready_at = 0        # AI đang hết lượt → chờ tới lúc này mới chạy tiếp
        note = ""
        while True:
            try:
                sh.refresh_cols()
                reqs = sh.read_requests()                 # yêu cầu từ menu trên sheet
                if reqs:
                    for rq in reqs:
                        try:
                            note = sh.handle_request(rq, last_try) or note
                        except Exception as e:
                            log("   ❌ Lỗi xử lý yêu cầu:", str(e)[:200])
                    sh.delete_meta_ids([rq.get("_id") for rq in reqs])
                if time.time() < ai_ready_at:
                    note = "AI hết lượt – tự chạy lại lúc " + time.strftime("%H:%M", time.localtime(ai_ready_at))
                elif note.startswith("AI hết lượt"):
                    note = ""
                sh.publish_progress(note)
                todo = [r for r, j in sh.jobs.items() if j.get("st") == "pending"]
                todo.sort(key=lambda r: (last_try.get(r, 0), r))   # dòng mới trước, dòng vừa lỗi xuống cuối
            except Exception as e:
                log("❌ Lỗi đọc sheet:", str(e)[:200])
                time.sleep(15)
                continue
            if todo and time.time() < ai_ready_at:
                todo = []      # AI đang hết lượt → đợi, không mở Chrome vô ích
            if todo:
                row = todo[0]
                last_try[row] = time.time()
                last_work = time.time()
                if context is None:
                    context = p.chromium.launch_persistent_context(
                        PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"])
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    process_row(sh, context, page, row)
                    if sh.jobs.get(row, {}).get("st") in ("running", "pending") and last_try.get(row):
                        if sh.jobs[row].get("st") == "running":
                            sh.jobs.pop(row, None)       # dòng bị xoá/đổi link giữa chừng → bỏ
                            sh.save_jobs()
                except QuotaDay as e:
                    wait = 30 * 60
                    resume = time.strftime("%H:%M", time.localtime(time.time() + wait))
                    log("   ⏸", e, f"– tạm nghỉ tới {resume}")
                    try:
                        sh.set_status(row, f"⏳ Chờ – AI hết lượt miễn phí, tự thử lại lúc {resume}")
                    except Exception:
                        pass
                    ai_ready_at = time.time() + wait
                except GeminiBusy as e:
                    log("   ⏸", e, f"– tạm nghỉ AI {RETRY_SEC}s, dòng này xếp xuống cuối")
                    try:
                        sh.set_status(row, "⏳ Chờ – AI quá tải, tự thử lại")
                    except Exception:
                        pass
                    ai_ready_at = time.time() + RETRY_SEC
                except Exception as e:
                    log("   ❌", str(e)[:200])
                    if "protected" in str(e).lower():
                        log(f"   → Ô bị KHOÁ. Vào Dữ liệu → Trang tính và dải ô được bảo vệ, thêm {SERVICE_EMAIL} vào quyền chỉnh sửa.")
                    try:
                        sh.set_status(row, "⚠️ Lỗi – " + str(e)[:120])
                    except Exception:
                        pass
                last_work = time.time()
                time.sleep(GAP_SEC)
                continue
            if context is not None and time.time() - last_work > CLOSE_CHROME_IDLE:
                context.close()
                context = None
                log("Rảnh – đã đóng Chrome, vẫn chờ việc.")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()