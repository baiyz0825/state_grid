"""敏感字段落盘加密的单元测试（离线，无需 Home Assistant 运行时）。

用法:
    .venv/bin/python tests/test_crypto.py

覆盖:
1. _enc_field / _dec_field 加解密往返 + 边界（空值 / 无密钥 / 明文兼容）。
2. save_data 写入的 .storage 字典里 password/llm_api_key/email_account 均为密文，
   且非敏感字段 llm_base_url 保持明文。
3. 用 save_data 产出的密文字典重建客户端，加载后三个敏感字段可正确解密回原文。
4. 向后兼容：存储里若仍是旧版明文（无 "enc:" 前缀），加载后保持明文不被破坏。
5. 解密失败（密文被截断 / 非法 base64）不崩溃，返回空串。
"""

from __future__ import annotations

import asyncio
import collections
import importlib
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "state_grid"


# ─────────────────────────────────────────────────────────────
# 1. 最小化 stub 掉 homeassistant（仅导入期用到，与 live_check.py 同思路）
# ─────────────────────────────────────────────────────────────
def _mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Store:
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

# 把 state_grid 目录挂成包 sg，使其内部相对导入可用
sg_pkg = types.ModuleType("sg")
sg_pkg.__path__ = [str(PKG_DIR)]
sys.modules["sg"] = sg_pkg

dc = importlib.import_module("sg.data_client")

# 捕获 save_data 实际要落盘的数据，避免触碰文件系统 / Store
_CAPTURED: dict = {}


async def _fake_save_to_store(hass, key, data):
    _CAPTURED[key] = data


dc.async_save_to_store = _fake_save_to_store
# __init__ 在加载到 llm_api_key 时会调用 configure_llm，测试里用 no-op 替身
dc._captcha_solver.configure_llm = lambda *a, **k: None


class FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}


# ─────────────────────────────────────────────────────────────
# 2. 测试
# ─────────────────────────────────────────────────────────────
KEY = "0123456789abcdef0123456789abcdef"  # 32 hex 字符 = 16 字节
KEY2 = "fedcba9876543210fedcba9876543210"


def _make_client(crypto_key, config=None):
    """构造一个属性齐全的客户端实例（config={} 走安全分支，不会触发 .get 报错）。"""
    cfg = config or collections.defaultdict(lambda: None)
    if not config:
        cfg[dc._Ao] = 12  # 避免空配置下 refresh_interval<12 触发 TypeError（被 __init__ 吞掉）
    client = dc.StateGridDataClient(hass=FakeHass(), config=cfg, crypto_key=crypto_key)
    # save_data 引用的全部实例属性
    client.keyCode = "kc"
    client.publicKey = "pk"
    client.accessToken = "at"
    client.refreshToken = "rt"
    client.token = "tk"
    client.userInfo = {}
    client.powerUserList = []
    client.doorAccountDict = {}
    client.is_debug = False
    client.account = "13800000000"
    client.refresh_interval = 12
    client.password = "my-secret-pw"
    client.llm_api_key = "sk-abcdef123456"
    client.llm_base_url = "https://ark.example.com/api/v3"
    client.llm_model = "doubao-pro"
    client.email_account = "user@example.com"
    client._rk001_cooldown_until = 0.0
    client._login_fail_cooldown_until = 0.0
    client.last_status = {"state": "ok", "message": "", "ts": 0.0}
    client.timestamp = 0
    return client


async def _run():
    fails = 0

    def check(cond, msg):
        nonlocal fails
        if cond:
            print(f"  [PASS] {msg}")
        else:
            fails += 1
            print(f"  [FAIL] {msg}")

    # ---- 2.1 _enc_field / _dec_field 纯函数 ----
    print("[1] _enc_field / _dec_field 纯函数")
    enc = dc._enc_field("hello", KEY)
    check(enc.startswith("enc:"), "加密后带 'enc:' 前缀")
    check(dc._dec_field(enc, KEY) == "hello", "解密可还原原文")
    check(dc._enc_field("", KEY) == "", "空字符串原样返回")
    check(dc._enc_field(None, KEY) is None, "None 原样返回")
    check(dc._enc_field("x", None) == "x", "无密钥时明文返回（不加密）")
    # 明文兼容：无前缀原样返回
    check(dc._dec_field("plaintext", KEY) == "plaintext", "无前缀明文原样返回（向后兼容）")
    # 解密失败：非法 base64 → 返回空串，不应抛异常
    check(dc._dec_field("enc:!!!notbase64", KEY) == "", "非法密文解密失败返回空串不崩溃")
    # 不同密钥加密结果不同
    check(dc._enc_field("hello", KEY) != dc._enc_field("hello", KEY2), "不同密钥密文不同")

    # ---- 2.2 save_data 落盘内容 ----
    print("[2] save_data 落盘内容")
    _CAPTURED.clear()
    client = _make_client(KEY)
    await client.save_data()
    stored = _CAPTURED.get("state_grid.config")
    check(stored is not None, "save_data 调用了 async_save_to_store")
    # 通过 store key 常量取出字段（与 data_client 内部一致）
    af = dc._AF
    check(isinstance(stored.get(af), str) and stored[af].startswith("enc:"), "password 落盘为密文")
    check(stored.get("llm_api_key", "").startswith("enc:"), "llm_api_key 落盘为密文")
    check(stored.get("email_account", "").startswith("enc:"), "email_account 落盘为密文")
    check(stored.get("llm_base_url") == "https://ark.example.com/api/v3", "llm_base_url 保持明文（非敏感）")
    # 密文不等于原文
    check(stored[af] != "my-secret-pw", "落盘密文不等于明文")

    # ---- 2.3 用密文字典重建客户端：加载后正确解密 ----
    print("[3] 密文字典重建客户端 → 解密回原文")
    reloaded = dc.StateGridDataClient(hass=FakeHass(), config=dict(stored), crypto_key=KEY)
    check(reloaded.password == "my-secret-pw", "重建后 password 解密正确")
    check(reloaded.llm_api_key == "sk-abcdef123456", "重建后 llm_api_key 解密正确")
    check(reloaded.email_account == "user@example.com", "重建后 email_account 解密正确")

    # ---- 2.4 向后兼容：旧版明文存储 ----
    print("[4] 向后兼容：旧版明文存储加载后保持明文")
    legacy = dict(stored)
    legacy[af] = "plain-pw"
    legacy["llm_api_key"] = "plain-key"
    legacy["email_account"] = "plain@example.com"
    legacy_client = dc.StateGridDataClient(hass=FakeHass(), config=legacy, crypto_key=KEY)
    check(legacy_client.password == "plain-pw", "旧版明文 password 原样加载")
    check(legacy_client.llm_api_key == "plain-key", "旧版明文 llm_api_key 原样加载")
    check(legacy_client.email_account == "plain@example.com", "旧版明文 email_account 原样加载")

    # ---- 2.5 往返一致性：reload 后再次 save，密文仍可解密 ----
    print("[5] 往返一致性")
    _CAPTURED.clear()
    await reloaded.save_data()
    stored2 = _CAPTURED.get("state_grid.config")
    re_reloaded = dc.StateGridDataClient(hass=FakeHass(), config=dict(stored2), crypto_key=KEY)
    check(re_reloaded.password == "my-secret-pw", "再次保存+加载 password 一致")
    check(re_reloaded.email_account == "user@example.com", "再次保存+加载 email_account 一致")

    print()
    if fails:
        print(f"结果: {fails} 个用例失败")
        return 1
    print("结果: 全部用例通过")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
