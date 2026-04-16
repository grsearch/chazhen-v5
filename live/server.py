"""
Flask API 服务

接口：
  GET  /               dashboard.html
  GET  /api/status     运行状态
  GET  /api/trades     历史交易
  GET  /api/config     当前配置
  POST /api/config     更新配置
  POST /api/start      启动
  POST /api/stop       停止
  POST /api/reset      重置（paper 模式）
  POST /api/scan       手动扫描
  POST /api/symbol/add     添加币种
  POST /api/symbol/remove  删除币种
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from engine import get_engine

try:
    from flask import Flask, jsonify, request, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("请先安装依赖: pip install flask flask-cors")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "data", "server.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

STATIC_DIR = os.path.dirname(os.path.dirname(__file__))


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/api/status")
def api_status():
    try:
        return jsonify(get_engine().get_status())
    except Exception as e:
        logger.exception("status error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
def api_trades():
    try:
        page  = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 100))
        all_t = get_engine().get_trades()
        start = (page - 1) * limit
        return jsonify({"trades": all_t[start:start+limit], "total": len(all_t)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["GET"])
def api_cfg_get():
    try:
        return jsonify(get_engine().get_config())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_cfg_post():
    try:
        data = request.get_json(force=True) or {}
        # 类型转换
        for f in ["position_size_usdt", "min_gain_24h", "min_volume_usdt"]:
            if f in data:
                data[f] = float(data[f])
        for f in ["max_positions"]:
            if f in data:
                data[f] = int(data[f])
        if "auto_scan" in data:
            data["auto_scan"] = bool(data["auto_scan"])
        return jsonify(get_engine().update_config(data))
    except Exception as e:
        logger.exception("config error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
def api_start():
    try:
        return jsonify(get_engine().start())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        return jsonify(get_engine().stop())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    try:
        return jsonify(get_engine().reset())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        return jsonify(get_engine().manual_scan())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/symbol/add", methods=["POST"])
def api_sym_add():
    try:
        data   = request.get_json(force=True) or {}
        symbol = data.get("symbol", "").strip().upper()
        if not symbol:
            return jsonify({"error": "symbol 不能为空"}), 400
        return jsonify(get_engine().add_symbol(symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/symbol/remove", methods=["POST"])
def api_sym_remove():
    try:
        data   = request.get_json(force=True) or {}
        symbol = data.get("symbol", "").strip().upper()
        return jsonify(get_engine().remove_symbol(symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    engine = get_engine()
    logger.info(
        f"引擎就绪 模式={engine.cfg.get('mode','paper')} "
        f"监控={len(engine.state.get('symbols',[]))}个"
    )
    if engine.state.get("running"):
        logger.info("上次未正常停止，自动恢复...")
        engine.start()

    logger.info(f"服务启动 → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
