# voice-hermes-plugin

[中文](./README.md) | [English](./README.en.md)

Xalgo Voice Hermes 平台插件。它通过 Agent Channel 语音通道协议把 Xalgo 眼镜接入
Hermes Agent。

## 功能

这个插件把 Xalgo 眼镜语音接入 Hermes Agent：

- 插件形态遵循 Hermes 平台插件规范：`plugin.yaml` + `__init__.py` + `adapter.py`
- 主动连接 Xalgo Voice Channel Server，不需要 Hermes 暴露公网端口
- 支持 8 位绑定码换长期 Channel Token
- 支持 WebSocket `connect` / `resume` / `ping` / `pong`
- 支持语音输入转 Hermes 消息、Hermes 回复转 Xalgo 语音输出
- 支持 streaming reply、delivery ack、voice interrupt、token rotate 和 binding revoked 控制事件

## 安装

### 一键安装

推荐直接从 GitHub 安装：

```bash
curl -fsSL https://raw.githubusercontent.com/leo-yli/voice-hermes-plugin/main/scripts/install.sh | bash
```

这个脚本会自动完成：

- 从 `https://github.com/leo-yli/voice-hermes-plugin.git` 克隆或更新插件
- 安装 Python 依赖 `httpx` 和 `websockets`
- 把插件安装到 `~/.hermes/plugins/xalgo-voice-platform`
- 自动把 `xalgo-voice-platform` 加入 `~/.hermes/config.yaml` 的 `plugins.enabled`
- 修改配置前会生成 `config.yaml.bak.<timestamp>` 备份

可选环境变量：

```bash
curl -fsSL https://raw.githubusercontent.com/leo-yli/voice-hermes-plugin/main/scripts/install.sh \
  | HERMES_HOME=/opt/hermes BRANCH=main bash
```

如果 Hermes 使用特定 Python 环境：

```bash
curl -fsSL https://raw.githubusercontent.com/leo-yli/voice-hermes-plugin/main/scripts/install.sh \
  | HERMES_PYTHON=/path/to/hermes/python bash
```

如果你只想安装插件、不安装依赖：

```bash
curl -fsSL https://raw.githubusercontent.com/leo-yli/voice-hermes-plugin/main/scripts/install.sh \
  | SKIP_DEPS=1 bash
```

### 手动安装

如果不能使用一键脚本，可以手动安装：

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/leo-yli/voice-hermes-plugin.git ~/.hermes/plugins/xalgo-voice-platform
python3 -m pip install -r ~/.hermes/plugins/xalgo-voice-platform/requirements.txt
```

然后编辑 `~/.hermes/config.yaml`，确保 `plugins.enabled` 包含插件名：

```yaml
plugins:
  enabled:
    - xalgo-voice-platform
```

如果已有其他插件，不要覆盖原列表，把 `xalgo-voice-platform` 追加进去即可。

### 升级

再次运行一键安装脚本即可拉取最新版本：

```bash
curl -fsSL https://raw.githubusercontent.com/leo-yli/voice-hermes-plugin/main/scripts/install.sh | bash
```

也可以手动更新：

```bash
git -C ~/.hermes/plugins/xalgo-voice-platform pull --ff-only
```

## 绑定 Xalgo 账号

运行 Hermes gateway setup，选择 `Xalgo Voice`：

```bash
hermes gateway setup
```

按提示输入：

- Xalgo App 里「连接 Hermes / Agent Channel」生成的 8 位绑定码
- 设备名称，例如 `Hermes on Mac`

绑定成功后，插件会把以下变量写入 `~/.hermes/.env`：

```bash
XALGO_VOICE_TOKEN=...
XALGO_VOICE_INSTANCE_ID=...
XALGO_VOICE_SERVER_URL=wss://...
XALGO_VOICE_BOUND_USER_ID=...
XALGO_VOICE_BOUND_USER_NAME=...
XALGO_VOICE_DEVICE_LABEL=...
```

打开想作为默认投递位置的 Xalgo Agent 对话后，发送一次：

```text
/sethome
```

Hermes 会把当前真实会话保存为 home channel，用于 cron 结果和跨平台消息投递。
这一步只需要做一次；如果看到 `No home channel is set for Xalgo_Voice` 提示，也用
`/sethome` 处理。

## 重启并验证

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
XALGO_VOICE_SERVER_URL=wss://asr-test.jlpay.com/agent-channel/connect
XALGO_VOICE_DEVICE_LABEL="Hermes on Mac"
```

可选项：

```bash
XALGO_VOICE_API_BASE_URL=https://asr-test.jlpay.com/api/v1/agent-channel
XALGO_VOICE_REPLY_MODE=voice_first  # voice_first | text_first | both
XALGO_VOICE_STREAMING=true
# 推荐在目标 Agent 对话里发送 /sethome 自动写入；不要手填伪默认值。
XALGO_VOICE_HOME_CHANNEL=<set-by-/sethome>
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
    "server_url": "wss://example.test/agent-channel/connect",
    "api_base_url": "https://example.test/api/v1/agent-channel",
})
inst = platform_registry.create_adapter("xalgo_voice", cfg)
print(inst.name, inst.settings.is_bound(), inst.platform.value)
PY
```
