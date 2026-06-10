"""
同花顺桥接服务器（运行在 32 位 Python 上）

用法：
    C:\Python310_x86\python.exe broker/ths_bridge_server.py

通过 HTTP API 暴露 easytrader 操控同花顺的功能，
解决 64 位 Python 无法操控 32 位同花顺客户端的问题。

API 端点：
    GET  /health           健康检查
    POST /connect          连接同花顺客户端
    POST /disconnect       断开连接
    GET  /account          获取账户信息
    GET  /positions        获取持仓列表
    POST /buy              买入
    POST /sell             卖出
    POST /cancel           撤单
    GET  /orders           获取今日委托
    GET  /quote/<code>     获取实时行情
"""

import csv
import io
import json
import os
import subprocess
import sys
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ---------- 全局 trader 实例 ----------
_trader = None
_connected = False


def _find_xiadan_pid():
    """查找 xiadan.exe 的进程 ID（取内存占用最大的，即主交易窗口）"""
    try:
        import csv as _csv
        result = subprocess.run(
            ['tasklist', '/fi', 'IMAGENAME eq xiadan.exe', '/fo', 'csv', '/nh'],
            capture_output=True, text=True, timeout=5
        )
        pids = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            reader = _csv.reader([line])
            parts = next(reader)
            if len(parts) >= 5 and parts[0].strip().lower() == 'xiadan.exe':
                pid = int(parts[1].strip())
                mem_str = parts[4].strip().replace(' K', '').replace(',', '')
                try:
                    mem = int(mem_str)
                except ValueError:
                    mem = 0
                pids.append((mem, pid))

        if pids:
            # 返回内存最大的（主交易窗口）
            pids.sort(reverse=True)
            print(f"[Bridge] Found xiadan.exe PIDs: {[(p, m) for m, p in pids]}, using PID={pids[0][1]}")
            return pids[0][1]
        return None
    except Exception as e:
        print(f"[Bridge] _find_xiadan_pid error: {e}")
        return None


def get_trader():
    global _trader
    if _trader is None:
        import easytrader
        _trader = easytrader.use('ths')
    return _trader


# ---------- 操作函数 ----------

def _setup_trader_with_app(trader, app):
    """设置 trader 的 _app 和 _main，处理弹窗"""
    import pywinauto

    # 关闭所有弹窗
    time.sleep(1)
    closed = 0
    for window in app.windows(class_name="#32770", visible_only=True):
        try:
            title = window.window_text()
            if title != trader.config.TITLE:
                print(f"[Bridge] Closing popup: \"{title}\"")
                window.close()
                closed += 1
                time.sleep(0.3)
        except:
            pass

    # 也关闭其他可能的弹窗
    if closed == 0:
        # 尝试找到并关闭验证码/提示框
        for window in app.windows(visible_only=True):
            try:
                title = window.window_text()
                cls = window.class_name()
                if title != trader.config.TITLE and cls == '#32770':
                    print(f"[Bridge] Closing dialog: \"{title}\"")
                    window.close()
                    time.sleep(0.3)
            except:
                pass

    time.sleep(1)

    # 找主交易窗口：包含 SysTreeView32 (ctrl_id=129) 的窗口
    main_window = None
    for window in app.windows():
        try:
            for child in window.children():
                if child.class_name() == 'SysTreeView32' and child.control_id() == 129:
                    # 获取 WindowSpecification
                    handle = window.handle
                    main_window = app.window(handle=handle)
                    break
        except:
            pass
        if main_window:
            break

    if main_window is None:
        main_window = app.top_window()

    trader._app = app
    trader._main = main_window

    # 初始化 toolbar
    try:
        trader._init_toolbar()
    except Exception as e:
        print(f"[Bridge] Warning: _init_toolbar failed: {e}")

    print(f"[Bridge] Main window: \"{main_window.window_text()}\" Class={main_window.class_name()}")


def do_connect(exe_path=None, pid=None):
    global _connected
    try:
        trader = get_trader()

        # 方式1: 按进程 PID 连接（最可靠）
        if pid:
            import pywinauto
            app = pywinauto.Application().connect(process=int(pid), timeout=10)
            _setup_trader_with_app(trader, app)
            _connected = True
            return {"success": True, "message": f"Connected to PID={pid}"}

        # 方式2: 自动查找 xiadan.exe PID
        found_pid = _find_xiadan_pid()
        if found_pid:
            import pywinauto
            app = pywinauto.Application().connect(process=found_pid, timeout=10)
            _setup_trader_with_app(trader, app)
            _connected = True
            return {"success": True, "message": f"Connected to PID={found_pid}"}

        # 方式3: 按路径连接
        if exe_path:
            trader.connect(exe_path)
        else:
            trader.connect()

        _connected = True
        return {"success": True, "message": "Connected to THS"}
    except Exception as e:
        _connected = False
        return {"success": False, "message": str(e)}


def do_disconnect():
    global _connected, _trader
    _connected = False
    _trader = None
    return {"success": True, "message": "Disconnected"}


def do_get_account():
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        balance = trader.balance
        if isinstance(balance, dict):
            return {"success": True, "data": balance}
        elif isinstance(balance, list) and len(balance) > 0:
            return {"success": True, "data": balance[0]}
        return {"success": True, "data": balance}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_get_positions():
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        positions = trader.position
        if isinstance(positions, list):
            return {"success": True, "data": positions}
        elif isinstance(positions, dict):
            return {"success": True, "data": list(positions.values())}
        return {"success": True, "data": positions}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_buy(code, price, qty):
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        result = trader.buy(code, float(price), int(qty))
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_sell(code, price, qty):
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        result = trader.sell(code, float(price), int(qty))
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_cancel(order_id):
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        if hasattr(trader, 'cancel_entrust'):
            result = trader.cancel_entrust(order_id)
        elif hasattr(trader, 'cancel'):
            result = trader.cancel(order_id)
        else:
            return {"success": False, "message": "Cancel not supported"}
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_get_orders():
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        if hasattr(trader, 'today_entrusts'):
            orders = trader.today_entrusts or []
        elif hasattr(trader, 'entrust'):
            orders = trader.entrust or []
        else:
            orders = []
        return {"success": True, "data": orders}
    except Exception as e:
        return {"success": False, "message": str(e)}


def do_get_quote(code):
    if not _connected:
        return {"success": False, "message": "Not connected"}
    try:
        trader = get_trader()
        if hasattr(trader, 'get_quote'):
            data = trader.get_quote(code)
            return {"success": True, "data": data}
        return {"success": False, "message": "get_quote not available"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ---------- HTTP Handler ----------

ROUTES = {
    ('GET', '/health'):       lambda p, b: {"success": True, "connected": _connected},
    ('POST', '/connect'):     lambda p, b: do_connect(b.get('exe_path', [None])[0] if b.get('exe_path') else None),
    ('POST', '/disconnect'):  lambda p, b: do_disconnect(),
    ('GET', '/account'):      lambda p, b: do_get_account(),
    ('GET', '/positions'):    lambda p, b: do_get_positions(),
    ('POST', '/buy'):         lambda p, b: do_buy(b.get('code', [''])[0], b.get('price', [0])[0], b.get('qty', [0])[0]),
    ('POST', '/sell'):        lambda p, b: do_sell(b.get('code', [''])[0], b.get('price', [0])[0], b.get('qty', [0])[0]),
    ('POST', '/cancel'):      lambda p, b: do_cancel(b.get('order_id', [''])[0]),
    ('GET', '/orders'):       lambda p, b: do_get_orders(),
}


class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0] if args else format}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _parse_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except:
            result = {}
            for k, v in parse_qs(raw.decode('utf-8')).items():
                result[k] = v[0] if len(v) == 1 else v
            return result

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        params = parse_qs(parsed.query)

        # Special: /quote/<code>
        if path.startswith('/quote/'):
            code = path.split('/quote/')[1]
            self._send_json(do_get_quote(code))
            return

        handler = ROUTES.get(('GET', path))
        if handler:
            try:
                result = handler(params, params)
                self._send_json(result)
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        else:
            self._send_json({"success": False, "message": f"Unknown GET {path}"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        body = self._parse_body()

        handler = ROUTES.get(('POST', path))
        if handler:
            try:
                result = handler({}, body)
                self._send_json(result)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"success": False, "message": str(e)}, 500)
        else:
            self._send_json({"success": False, "message": f"Unknown POST {path}"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


# ---------- 启动 ----------

def main():
    port = int(os.environ.get('THS_BRIDGE_PORT', 18888))
    host = os.environ.get('THS_BRIDGE_HOST', '127.0.0.1')

    print(f"THS Bridge Server starting on {host}:{port} (32-bit Python)")
    print(f"Easytrader path: {get_trader() is not None and 'OK' or 'FAILED'}")

    server = HTTPServer((host, port), BridgeHandler)
    print(f"Listening at http://{host}:{port}")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
