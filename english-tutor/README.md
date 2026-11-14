# AI 英语口语陪练机 · 原型

一个儿童英语口语陪练机的原型，在网页和 ESP32 硬件上都能用：孩子直接开口说英语，AI 老师实时语音回应，话说一半可以打断，语法错误会用“复述”的方式温和纠正。

```
 孩子说话
   │  麦克风（硬件双麦 + AEC / 浏览器回声消除）
   ▼
 ESP32-S3（xiaozhi 开源固件）  或  网页测试页
   │  Opus 16kHz 60ms / PCM16             ← 本仓库 server/ 负责协议转换、打断、节奏控制
   ▼
 本服务 server/  ──WebSocket──►  阿里云百炼 Qwen-Omni-Realtime（端到端语音大模型）
                                  听懂 + 思考 + 说话 一个模型完成，流式返回
```

## 一、模型怎么选：Qwen-Omni-Flash-Realtime（阿里云百炼）

要同时做到“流畅、低延迟、听得清小孩、价格亲民”，**端到端实时语音模型**比“ASR → 大模型 → TTS”三段拼接更合适：

- **延迟低**：音频进、音频出，一个模型完成，省掉三段串行的等待，边生成边播放。
- **支持打断**：服务端 VAD 检测到孩子开口，当前回答立即停止。
- **听得懂孩子**：模型直接“听”原始音频，含糊发音、中英夹杂都能结合语境理解，不会因为 ASR 先把字识别错而跟着答偏。

国内主流的两个端到端方案，官方按量价格对比如下（元/百万 token，北京地域）：

| | 输入音频 | 输出音频 |
|---|---|---|
| **阿里云 qwen3-omni-flash-realtime** | **18.9** | **75.1**（文本+音频，文本不计费）|
| 火山引擎 豆包端到端实时语音大模型 | 80 | 300 |

Qwen 的单价大约是豆包的 **1/4**，所以默认选它。还有几点：

- 默认模型是最新的 `qwen3.8-omni-flash-realtime`，阿里官方 Python SDK 当前的示例就用这个模型。它的实时版官方价格我在开发环境里没法访问阿里云文档核实，**请在[百炼控制台](https://bailian.console.aliyun.com/)确认**。如果它更贵，在 `.env` 里改成 `MODEL=qwen3-omni-flash-realtime`（上面表格里的价格就是这个型号的）即可，两代协议差异本服务已经做了适配。
- 服务端每一轮都会在日志里打印 `usage`（实际 token 数）和**首包延迟**，跑一段真实对话就能算出“一个孩子一天练 20 分钟要多少钱”，不用估。
- 整套代码对模型厂商做了抽象（`server/realtime.py`），以后想换豆包或其他实时模型，只需新增一个 Provider。

## 二、“听清孩子说话”靠的是什么

这个问题一半在硬件，一半在参数：

1. **硬件**：用双麦克风加回声消除（AEC），麦克风靠近孩子（离嘴 20～50cm），喇叭和麦克风在结构上隔开。这些比换模型更重要。
2. **VAD 参数**（已按儿童调好，见 `.env.example`）：`VAD_THRESHOLD=0.3`（声音小也能触发），`VAD_SILENCE_MS=900`（孩子想单词时停顿长，不要急着截断）。
3. **低龄或嘈杂环境用按键模式**：设 `TURN_MODE=push_to_talk`，按住说话、松开才回答，彻底避免误触发和截断。
4. **提示词**（`server/prompts.py`）：要求模型“孩子发音含糊时按最可能的意思友好理解，实在听不懂就俏皮地请他再说一遍”，中英夹杂时先教英文说法再让孩子跟读。

## 三、快速开始（先用电脑浏览器体验）

```bash
cd english-tutor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # 需要系统有 libopus：apt install libopus0 / brew install opus
cp .env.example .env                   # 填入 DASHSCOPE_API_KEY
python -m server.app
```

打开 http://localhost:8000 ，点“开始对话”，允许麦克风后直接说英语。

- 没有 API Key 也能先跑通流程：`PROVIDER=mock python -m server.app`。mock 模式下回答只是一声“嘀”和固定的字幕。
- 用手机浏览器访问需要 HTTPS，否则浏览器不给麦克风权限。可以用 nginx 或 caddy 加证书反向代理，或者先在电脑上用 localhost 测。

运行测试：`pip install -r requirements-dev.txt && pytest`

### 把对话功能加到你自己的网页里（比如 Codex 做的网页）

在你网页 HTML 的 `</body>` 前面加一行：

```html
<script src="http://localhost:8000/static/tutor-widget.js" data-title="Lily 老师"></script>
```

页面右下角会出现“🎤 和 AI 老师说英语”按钮，点开就能对话。组件的样式是隔离的，不会影响你原来的页面。

- 先运行 `python -m server.app`，再打开你的网页。两个都在本机时用 localhost，不需要 HTTPS。
- API Key 只放在 `.env` 里，网页里不需要也不应该出现 Key。
- 想自己设计界面：引入 `http://localhost:8000/static/tutor-client.js`，用 `EnglishTutor.create()` 拿到事件和音频，用法见文件开头的注释。

## 四、接上真实硬件（ESP32-S3 + xiaozhi 开源固件）

设备端直接用开源固件 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)。它已经做好了唤醒词、按键、Opus 编解码、WiFi 配网、屏幕表情和 AEC。本服务实现了它的 WebSocket 协议，设备连上来就能用，**不用自己写固件**。

### 推荐硬件

**要回声消除和可打断，必须用带 ES7210 + ES8311 的板子，比如立创·实战派 ESP32-S3。** 纯面包板方案（INMP441 + MAX98357A）在 xiaozhi 固件里不支持回声消除。选购链接、接线和烧录步骤见 [HARDWARE.md](HARDWARE.md)。

### 让设备连到本服务

1. 按 xiaozhi-esp32 的文档装好 ESP-IDF，选择你的板型。
2. 在 `idf.py menuconfig` 的 xiaozhi 配置菜单里，把 **OTA 地址**改成 `http://<你的服务器IP>:8000/xiaozhi/ota/`，然后编译烧录。
3. 设备开机会请求这个 OTA 地址。本服务返回 WebSocket 地址 `ws://<IP>:8000/xiaozhi/v1/` 和 `DEVICE_TOKEN`，设备随后自动连接，不需要激活码。
4. 说唤醒词或按键，就可以开始和英语老师对话。

几点说明：

- 设备在公网时，在 `.env` 里设置 `PUBLIC_WS_URL`，生产环境要用 `wss://`。
- 想“边说边打断”，要用支持 AEC 的板子，并在固件里开启实时对话模式。普通板子是半双工：老师说完才能说，按键可以打断。
- 设备协议、Opus 打包和打断逻辑都在 `server/device.py`，测试里用模拟设备覆盖了：免按键对话、按键对话、唤醒词问候、打断后不再出声。

## 五、目录

```
english-tutor/
├── server/
│   ├── app.py        # 入口：网页、浏览器 WS、设备 WS、OTA
│   ├── realtime.py   # 实时语音模型客户端（Qwen-Omni + 离线 Mock），统一事件格式
│   ├── device.py     # xiaozhi 设备协议：Opus 编解码、24k 下行、节奏控制、打断
│   ├── browser.py    # 网页测试页网关
│   ├── bridge.py     # 共用：打断、首包延迟统计、字幕分句
│   ├── prompts.py    # 儿童英语陪练提示词（分级、温和纠错、安全规则）
│   └── config.py     # 所有配置（环境变量）
├── web/index.html    # 网页测试版（手机/电脑）
└── tests/            # 端到端测试（mock 模型 + 模拟设备 + 假百炼服务器）
```

## 六、下一步可以做什么

- **发音评测打分**：接讯飞或腾讯智聆的口语评测 API，对孩子的跟读按音素和单词打分。
- **课程和场景**：按年龄和级别设计主题课，比如动物、超市购物，在提示词里加当天的单词表。
- **家长端**：记录每次对话（日志里已有转写），生成学习报告：说了多少句、新学了哪些词、常犯什么错。
- **合规**：儿童数据保护（不存原始录音或加密存储）、内容安全二次过滤，硬件还要过 SRRC 和 3C。

## 已知限制

- 在开发环境里无法访问阿里云文档和真实接口，Qwen 协议是按**阿里官方 Python SDK（dashscope 2.0.4）源码**实现的，并用假服务器测试过收发格式。**首次接真实 Key 时请看日志确认 `session.updated` 正常返回**，如有字段报错按 `error` 日志调整 `server/realtime.py` 里的 `session_payload()`。
- 首包延迟主要取决于模型和网络。服务端自身的开销在 mock 测试中约为个位数毫秒，真实延迟以日志为准。
