# voice-hermes-plugin

[中文](./README.md) | [English](./README.en.md)

Xalgo Voice Hermes 平台插件。它通过 `voice-openclaw-plugin` 同款 Xalgo 语音通道协议，
把 Xalgo 眼镜接入 Hermes Agent。

## 功能

这个插件把 Xalgo 眼镜语音接入 Hermes Agent：

- 插件形态遵循 Hermes 平台插件规范：`plugin.yaml` + `__init__.py` + `adapter.py`
- 主动连接 Xalgo Voice Channel Server，不需要 Hermes 暴露公网端口
- 支持 8 位绑定码换长期 Channel Token
- 支持 WebSocket `connect` / `resume` / `ping` / `pong`
- 支持语音输入转 Hermes 消息、Hermes 回复转 Xalgo 语音输出
- 支持 streaming reply、delivery ack、voice interrupt、token rotate 和 binding revoked 控制事件

## 安装

### 1. 安装 Python 依赖

如果你的 Hermes 环境已经包含 `httpx` 和 `websockets`，可以跳过这一步。

```bash
pip install -r /Users/leo/project/voice-hermes-plugin/requirements.txt
```

或手动安装：

```bash
pip install httpx websockets
```

### 2. 安装插件到 Hermes

推荐使用软链接，方便本地修改后直接生效：

```bash
mkdir -p ~/.hermes/plugins
ln -s /Users/leo/project/voice-hermes-plugin ~/.hermes/plugins/xalgo-voice-platform
```

如果目标机器不能访问当前项目目录，也可以复制：

```bash
mkdir -p ~/.hermes/plugins/xalgo-voice-platform
cp -R /Users/leo/project/voice-hermes-plugin/* ~/.hermes/plugins/xalgo-voice-platform/
```

### 3. 启用插件

编辑 `~/.hermes/config.yaml`，确保 `plugins.enabled` 包含插件名：

```yaml
plugins:
  enabled:
    - xalgo-voice-platform
```

如果已有其他插件，不要覆盖原列表，把 `xalgo-voice-platform` 追加进去即可。

### 4. 绑定 Xalgo 账号

运行 Hermes gateway setup，选择 `Xalgo Voice`：

```bash
hermes gateway setup
```

按提示输入：

- Xalgo REST API base URL，默认读取本项目 `endpoints.json`
- Xalgo App 里「连接 Hermes / OpenClaw」生成的 8 位绑定码
- 设备名称，例如 `Hermes on Mac`

绑定成功后，插件会把以下变量写入 `~/.hermes/.env`：

```bash
XALGO_VOICE_TOKEN=...
XALGO_VOICE_INSTANCE_ID=...
XALGO_VOICE_SERVER_URL=wss://...
XALGO_VOICE_API_BASE_URL=https://...
XALGO_VOICE_BOUND_USER_ID=...
XALGO_VOICE_BOUND_USER_NAME=...
XALGO_VOICE_DEVICE_LABEL=...
```

### 5. 重启并验证

重启 Hermes gateway：

```bash
hermes gateway restart
```

查看状态：

```bash
hermes gateway status
```

日志里应能看到 Xalgo Voice 平台加载、WebSocket connected / authenticated
一类信息。之后通过 Xalgo 眼镜说话即可触发 Hermes Agent。

## 手动配置

如果不使用 setup，也可以直接在 `~/.hermes/.env` 写入：

```bash
XALGO_VOICE_TOKEN=<channel-token>
XALGO_VOICE_INSTANCE_ID=<stable-instance-id>
XALGO_VOICE_SERVER_URL=wss://asr-test.jlpay.com/openclaw/connect
XALGO_VOICE_API_BASE_URL=https://asr-test.jlpay.com
XALGO_VOICE_DEVICE_LABEL="Hermes on Mac"
```

可选项：

```bash
XALGO_VOICE_REPLY_MODE=voice_first  # voice_first | text_first | both
XALGO_VOICE_STREAMING=true
XALGO_VOICE_HOME_CHANNEL=xalgo:user:default
```

## 开发验证

```bash
python -m pytest -q
python -m py_compile adapter.py __init__.py
```

如果要在本地 Hermes 源码环境下做注册烟测：

```bash
uv run --project /tmp/hermes-agent python - <<'PY'
import sys
sys.path.insert(0, "/Users/leo/project/voice-hermes-plugin")
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry, PlatformEntry
import adapter

class Ctx:
    def register_platform(self, **kw):
        platform_registry.register(PlatformEntry(
            name=kw["name"],
            label=kw["label"],
            adapter_factory=kw["adapter_factory"],
            check_fn=kw["check_fn"],
            validate_config=kw["validate_config"],
            required_env=kw["required_env"],
            source="plugin",
        ))

adapter.register(Ctx())
cfg = PlatformConfig(enabled=True, extra={
    "token": "tok",
    "instance_id": "hermes_test",
    "server_url": "wss://example.test/openclaw/connect",
    "api_base_url": "https://example.test",
})
inst = platform_registry.create_adapter("xalgo_voice", cfg)
print(inst.name, inst.settings.is_bound(), inst.platform.value)
PY
```
