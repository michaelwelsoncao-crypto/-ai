# 硬件选购与烧录指南

目标：**可以打断（孩子随时插话）、有回声消除（AEC）、不需要屏幕、成本尽量低**。

## 关键结论：纯面包板方案做不到回声消除

我核对了 xiaozhi-esp32 固件源码（`main/Kconfig.projbuild`）：

- 设备端回声消除的开关叫 `Enable Device-Side AEC`（`CONFIG_USE_DEVICE_AEC`），**只对一份白名单里的板子开放**。固件说明里写明，它需要“扬声器信号的干净参考通路”，以及麦克风和喇叭之间的物理隔离。
- 面包板板型 `bread-compact-wifi`（INMP441 麦克风 + MAX98357A 功放）**不在白名单里**。它的麦克风和喇叭是两路独立的 I2S，拿不到回声参考信号，所以老师说话时孩子插话会被自己的回声干扰。
- 白名单里的板子基本都用 **ES7210（多路麦克风 ADC，带回声参考）+ ES8311（音频编解码）** 这对芯片，例如乐鑫 ESP32-S3-BOX-3、**立创·实战派 ESP32-S3**（`LICHUANG_DEV_S3`）、ESP32-S3-Korvo-2，以及微雪的多款 S3 板。

所以要“可打断 + 回声消除”，**原型请直接买白名单里带 ES7210 + ES8311 的板子**，不要买 INMP441 面包板套件。

## 推荐购买（原型）

| 优先级 | 板子 | 为什么 | 淘宝搜索 |
|---|---|---|---|
| ★ 首选 | **立创·实战派 ESP32-S3** | 在 xiaozhi 的 AEC 白名单里；立创的板子一般会在立创开源硬件平台公开原理图，量产时可以照着画一块去掉屏幕的板子（是否开源请到平台确认） | [搜索](https://s.taobao.com/search?q=%E7%AB%8B%E5%88%9B%E5%AE%9E%E6%88%98%E6%B4%BE%20ESP32-S3) |
| 备选 | 其他“ESP32-S3 + ES7210 + ES8311”的小智开发板 | 买前**一定要问卖家**：“支持小智固件的设备端 AEC（实时对话、可打断）吗？对应 xiaozhi 的哪个板型？” | [搜索](https://s.taobao.com/search?q=ESP32-S3%20ES7210%20ES8311%20%E5%B0%8F%E6%99%BA%20%E5%9B%9E%E5%A3%B0%E6%B6%88%E9%99%A4%20%E5%BC%80%E5%8F%91%E6%9D%BF) |
| 备选 | 乐鑫 ESP32-S3-BOX-3 | 官方套件，最稳，但贵（约 300 元） | — |

价格我没法实时查，以淘宝实际为准。屏幕不用管：开发板上带着也没关系，固件照样跑；正式产品再去掉屏幕。

另外准备一根能传数据的 **USB-C 数据线**（有些线只能充电，没法烧录）。

> 如果只想先花几十元熟悉流程、暂时不需要打断，可以买面包板套件：[ESP32-S3-N16R8](https://s.taobao.com/search?q=ESP32-S3-N16R8%20%E5%BC%80%E5%8F%91%E6%9D%BF)、[INMP441](https://s.taobao.com/search?q=INMP441)、[MAX98357A](https://s.taobao.com/search?q=MAX98357A)、[3W 4Ω 喇叭](https://s.taobao.com/search?q=3W%20%E5%96%87%E5%8F%AD%204%E6%AC%A7)、[面包板和杜邦线](https://s.taobao.com/search?q=%E9%9D%A2%E5%8C%85%E6%9D%BF%20%E6%9D%9C%E9%82%A6%E7%BA%BF%20%E5%A5%97%E8%A3%85)。这套是**半双工**：老师说完孩子才能说，按 BOOT 键可以打断。
>
> 接线（来自固件 `boards/bread-compact-wifi/config.h`）：
> INMP441：WS→GPIO4，SCK→GPIO5，SD→GPIO6，L/R→GND，VDD→3.3V
> MAX98357A：DIN→GPIO7，BCLK→GPIO15，LRC→GPIO16，VIN→5V
> 按键：BOOT=GPIO0；音量+=GPIO40，音量-=GPIO39

## 烧录 xiaozhi 固件并连到我们的服务器

1. 安装 ESP-IDF（Windows 用乐鑫官方的一键安装器，版本按 xiaozhi README 要求）。
2. 下载固件：`git clone https://github.com/78/xiaozhi-esp32.git`
3. `idf.py set-target esp32s3`，然后 `idf.py menuconfig`，进入 **Xiaozhi Assistant** 菜单：
   - **Board Type**：选你买的板子，比如立创实战派选 `LICHUANG_DEV_S3` 那一项。
   - **Default OTA URL**：改成 `http://<运行 server 的电脑的局域网 IP>:8000/xiaozhi/ota/`
   - **Enable Device-Side AEC**：勾上。这一步就是回声消除加可打断。
   - 可选 **Enable Wake Word Detection in Listening Mode**：勾上后孩子喊唤醒词也能打断。
4. `idf.py build flash monitor`
5. 第一次开机，设备会开一个 WiFi 热点。用手机连上，在配网页里填家里的 WiFi。
6. 电脑上运行 `python -m server.app`。电脑和设备要在同一个 WiFi 下；电脑防火墙要放行 8000 端口。
7. 对设备说唤醒词或按键，就可以开始对话。打开 AEC 后，老师说话时孩子可以直接插话，服务端检测到孩子开口会立刻停掉老师的声音。

## 调试小贴士

- 老师说话时自己被打断（回声没消干净）：在 `.env` 里把 `VAD_THRESHOLD` 调高到 0.5 左右，喇叭音量调小一点，让麦克风离喇叭远一些。
- 孩子小声说话没反应：把 `VAD_THRESHOLD` 调低到 0.2，让孩子离麦克风近一些。
- 服务端日志里有 `device ... connected`、`孩子: ...`、`首包延迟` 这几行，说明整条链路已经通了。

## 量产时（去掉屏幕）

参考开发板的原理图（立创实战派如果公开了原理图，可以直接参考），保留 **ESP32-S3-N16R8 模组 + ES7210 + ES8311 + 双麦 + 功放 + 锂电充电**，去掉屏幕和摄像头，请方案商或画板工程师画成一块圆形小板。在 xiaozhi 里新建一个板型，复制实战派的配置，把显示改成 NoDisplay。
