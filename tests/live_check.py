"""本地联调脚本：脱离 Home Assistant，直接跑通「登录 → 拉取国家电网数据」全链路。

用法:
    .venv/bin/python tests/live_check.py \
        --phone 15686253002 --password 'xxx' \
        --llm-base-url https://api.deepseek.com \
        --llm-api-key sk-xxx --llm-model deepseek-chat

也可直接用环境变量 SG_PHONE / SG_PASSWORD / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。

说明:
- 只 stub 掉 homeassistant 的辅助模块（Store / JSONEncoder / aiohttp_client），
  data_client 的业务代码与网络请求全部是真实的。
- 账号信息不落盘到仓库，session 写到 .local_store.json（已 gitignore）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "state_grid"
STORE_FILE = ROOT / ".local_store.json"


# ─────────────────────────────────────────────────────────────
# 1. 用最小桩替换 homeassistant 依赖（仅导入期用到）
# ─────────────────────────────────────────────────────────────
def _mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Store:  # noqa: D401 - 仅占位，实际存储走本地 JSON
    def __init__(self, *args, **kwargs):
        pass


class _JSONEncoder(json.JSONEncoder):
    pass


_mod("homeassistant")
_mod("homeassistant.helpers")
_mod("homeassistant.util")
_mod("homeassistant.helpers.json", JSONEncoder=_JSONEncoder)
_mod("homeassistant.helpers.storage", Store=_Store)
_mod("homeassistant.util.json", load_json=json.load)
_mod("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda *a, **k: None)

import aiohttp  # noqa: E402

# 把 state_grid 目录挂成包 sg，使其内部相对导入可用
sg_pkg = types.ModuleType("sg")
sg_pkg.__path__ = [str(PKG_DIR)]
sys.modules["sg"] = sg_pkg

dc_mod = importlib.import_module("sg.data_client")
solver = importlib.import_module("sg.click_captcha_solver")
StateGridDataClient = dc_mod.StateGridDataClient

# 真实 aiohttp session
_SESSION: aiohttp.ClientSession | None = None


def _get_clientsession(hass=None, verify_ssl=True, **kwargs):
    """与 HA 的 async_get_clientsession 一致：同步返回 session 对象。"""
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    return _SESSION


dc_mod.async_get_clientsession = _get_clientsession


async def _async_save_to_store(hass, key, data):
    STORE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


dc_mod.async_save_to_store = _async_save_to_store


class FakeHass:
    """最小 hass 替身：只需要 async_add_executor_job。"""

    def __init__(self) -> None:
        self.data: dict = {}

    async def async_add_executor_job(self, target, *args):
        return await asyncio.to_thread(target, *args)


# ─────────────────────────────────────────────────────────────
# 2. 主流程
# ─────────────────────────────────────────────────────────────
def _summary(accounts) -> list[dict]:
    out = []
    for acc in accounts:
        out.append(
            {
                "户号": acc.get("consNo_dst") or acc.get("consNo"),
                "户名": acc.get("userName") or acc.get("loginAccount"),
                "余额(元)": acc.get("balance"),
                "昨日用电(kWh)": acc.get("daily_ele_num"),
                "本月用电(kWh)": acc.get("month_ele_num"),
                "今年用电(kWh)": acc.get("year_ele_num"),
                "刷新时间": acc.get("refresh_time"),
            }
        )
    return out


async def run(args) -> int:
    client = StateGridDataClient(hass=FakeHass(), config=None)
    client.llm_api_key = args.llm_api_key
    client.llm_base_url = args.llm_base_url
    client.llm_model = args.llm_model
    client.is_debug = args.debug
    if args.llm_api_key:
        solver.configure_llm(args.llm_api_key, args.llm_base_url, args.llm_model)

    print(f"[1/4] 开始登录: {args.phone} (LLM={args.llm_model} @ {args.llm_base_url})")
    result = await client.password_login(args.phone, args.password, encode=False, retry=args.retry)
    print(f"[2/4] 登录结果: {result}")
    print(f"[状态机] last_status = {client.last_status}")
    if not isinstance(result, dict) or result.get("errcode") != 0:
        errmsg = result.get("errmsg", "") if isinstance(result, dict) else str(result)
        print("[诊断] 登录失败:", errmsg)
        if "MAINTENANCE" in errmsg or "维护" in errmsg:
            print("       原因: 95598 网站正在升级维护（官方公告页），与账号/密码无关。")
        elif "WAF_BLOCKED" in errmsg or "风控" in errmsg:
            print("       原因: /api/* 被网关拦截（HTTP 405），")
            print("             95598 网站升级维护期间会直接拒绝 API 请求，与账号/密码无关。")

        # 演示：智能更新的「轻量探测」——仅请求 get_request_key，不触发登录/验证码/LLM
        print("[3/4] 触发轻量探测 _probe_site_state()（不触发登录，不消耗 LLM）...")
        probe = await client._probe_site_state()
        print(f"      探测结果: {probe}")

        # 演示：智能更新在维护期识别状态并跳过登录，仅保留上次数据
        print("[4/4] 模拟「自动智能更新」识别维护：refresh_data 应只探测并跳过登录...")
        client.last_status = probe
        await client.refresh_data(force_refresh=True)
        print(f"      维护识别后 last_status = {client.last_status}")
        print("      => coordinator 会把轮询间隔从 5 分钟拉长到 15 分钟")
        return 1

    print("[3/4] 拉取用电数据 ...")
    await client.refresh_data(force_refresh=True)
    print(f"[状态机] 刷新后 last_status = {client.last_status}")

    accounts = client.get_door_account_list()
    if not accounts:
        print("未获取到任何户号数据。")
        return 2

    print("\n===== 国家电网数据 =====")
    print(json.dumps(_summary(accounts), ensure_ascii=False, indent=2))
    print(f"\n户号数量: {len(accounts)}，数据已写入 {STORE_FILE}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--phone", default=os.getenv("SG_PHONE", ""))
    p.add_argument("--password", default=os.getenv("SG_PASSWORD", ""))
    p.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"))
    p.add_argument("--llm-api-key", default=os.getenv("LLM_API_KEY", ""))
    p.add_argument("--llm-model", default=os.getenv("LLM_MODEL", "deepseek-chat"))
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.phone or not args.password:
        print("缺少账号/密码（--phone / --password 或 SG_PHONE / SG_PASSWORD）")
        return 1

    try:
        return asyncio.run(run(args))
    finally:
        if _SESSION is not None and not _SESSION.closed:
            asyncio.run(_SESSION.close())


if __name__ == "__main__":
    sys.exit(main())
