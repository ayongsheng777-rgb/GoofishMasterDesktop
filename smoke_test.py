# -*- coding: utf-8 -*-
"""冒烟测试：启动 3 个非浏览器服务（不含 spider，需 Playwright 浏览器），验证可 boot。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launcher

# 仅测非浏览器服务
launcher.SERVICES = [
    s for s in launcher.SERVICES if s["name"] != "spider-service"
]

launcher.start_all()
print("\n===== 冒烟结果 =====")
all_ok = True
for svc in launcher.SERVICES:
    port = launcher.CFG["ports"][svc["port_key"]]
    ok = launcher.health_ok(port)
    all_ok = all_ok and ok
    print(f"  {svc['name']:16s} port={port} health={'✓' if ok else '✗'}")
    if not ok:
        logp = Path("data/logs") / f"{svc['name']}.log"
        if logp.exists():
            tail = logp.read_text(encoding="utf-8", errors="ignore").splitlines()[-15:]
            print("  --- 末 15 行日志 ---")
            print("\n".join("  " + l for l in tail))
time.sleep(1)
launcher.stop_all()
print("\n冒烟结论：", "全部就绪 ✓" if all_ok else "部分未就绪（多为缺少后端/Key 的预期降级）")
sys.exit(0 if all_ok else 1)
