# 无畏契约 HUD — Valorant 计分板 OCR + 游戏内叠层

《无畏契约》（Valorant）对局中实时读取**计分板**的 OCR 工具：后台截取游戏窗口，
识别每一行的玩家名 / 干员 / 击杀死亡助攻 / 大招 / 护甲 / 武器 / 爆能器，经
WebSocket 广播给 HUD，以**置顶透明叠层**按游戏内风格渲染在屏幕上；也可导出
分层 PNG 供 OBS 叠加，或让第二台机器作"接收端"独立显示。

```
            识别端（发送端）                      接收端
  valorant_hud.py  ──WS 广播──►  broadcast_server.py
  捕获计分板帧 ── OCR/模板 ──►   ──►  hud_overlay.py (置顶透明 HUD)
                                    ──►  PlayerCard 分层 PNG (OBS)
```

## 功能

- **发送端 / 接收端双模式**：发送端注入 Tab 展开计分板并识别；接收端显示 HUD（同机或远程）。
- **置顶透明 HUD**：头像、名字、击杀-死亡-助攻、大招（点数进度 / 就绪 / 爆能器）、护甲、武器。
- 死亡 backdrop、大招扫动动画、大招图标与干员对应；**换人 + 开局即死等边界场景不会残留上一位的旧大招图标**。
- 按已排定的名单**逐行盖章身份**（多人/换人/名字与头像对应由模板匹配 + 重排逻辑保证）。
- 帧级防抖：坏帧不整版闪死；黑帧过滤；失焦预览回退屏幕捕获等（`capture/`、`recognition/`）。
- 分层渲染导出（透明背景 PNG，供 OBS 直播叠加）。

## 目录结构

```
valorant_hud.py       主程序入口（发送端：OCR 识别 + WebSocket 广播 + 主窗口）
hud_overlay.py        HUD 叠层 / 卡片分层渲染（可独立 --export / --url 运行）
broadcast_server.py   WebSocket 广播服务（接收端核心）
preview_dialog.py     计分板范围预览对话框
ui_main_window.py     主窗口 UI（由 main_window.ui 用 pyside6-uic 生成）
ui_preview_dialog.py  预览框 UI（由 preview_dialog.ui 生成）
config/               配置加载/保存、layout 结构校验（config.json 为模板）
capture/              窗口捕获（PrintWindow / mss 屏幕回退）、Tab 注入、找窗
recognition/          OCR 后处理：计分板解析、身份重排、大招逻辑（engine/worker/scoreboard/ult_costs）
ocr/                  PaddleOCR 封装（PP-OCRv6_small_det / PP-OCRv6_small_rec）
opencv/               模板匹配封装：干员头像 / 武器 / 护甲 / 阵营 / 存活 / 爆能器
receiver/             接收端：WebSocket 客户端 + 对局状态模型
output/               JSON 写出（scoreboard/init；运行产物已 gitignore）
images/               模板素材（干员头像、武器、护甲、大招图标、backdrop 等）
icon.png              应用图标
valorant_hud.spec     PyInstaller 打包配置（单一通用版，覆盖 40/50 系显卡）
run_dev.bat           开发启动（自动 UAC 提权；解释器：%PYTHON% > py > python）
requirements.txt      运行依赖
```

改 UI：`pyside6-uic main_window.ui -o ui_main_window.py`（或 preview_dialog.ui → ui_preview_dialog.py）。

## 依赖 / 安装

Python 3.10+。OCR 默认 **GPU 推理**（`config.json` → `ocr.device: "gpu"`）。

```bash
pip install -r requirements.txt
```

- 识别模型按名**自动下载**到 `~/.paddlex`（首次需联网）。离线打安装包时把它们
  拷进仓库 `paddlex_cache/official_models/`（被 gitignore，见 spec 顶部注释）。
- CPU 机器：把 `paddlepaddle-gpu` 换成 `paddlepaddle`，并把 `config.json` 的 `ocr.device` 改为 `"cpu"`。

## 运行

- **发送端（读计分板）**：管理员权限运行 `valorant_hud.py` —— Tab 注入需管理员，
  双击 `run_dev.bat` 会自动提权（UAC）。进游戏后窗口标题含 `VALORANT` 即可开始采集。
- **接收端（显示 HUD）**：程序内切到「作为接收端」页连接广播地址；或直接
  `python hud_overlay.py --url ws://127.0.0.1:<port> --export out/` 导出分层 PNG。
- 命令行入口支持 `python hud_overlay.py --help` 查看（capture 相关需在游戏运行的本机执行）。

### 布局标定

`config/config.json` 中 `layout` 默认为 **`null`**：作者的个人标定（分辨率/窗口/行列表）
在公开发布时已移除。识别依赖 `rows / cols / col_fields / row_slots` 归一化布局
（结构见 `config/__init__.py` 的 `validate_layout`）。未标定时主程序会提示
"未标定计分板布局"。请用你的对局画面自行标定后写入 `config.json`——
完整行/列校准工具随作者发行版提供（`calibration/`，未包含在本仓库源码快照中）。

## 打包（PyInstaller）

```bash
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build valorant_hud.spec
```

单一通用版：paddle 官方 wheel 为 multi-arch，同一产物覆盖 40 系（sm_89）与 50 系（sm_120）。
产物以**管理员**身份运行（`uac_admin=True`）。详见 `valorant_hud.spec` 顶部注释。

## 免责声明

- 仅供**学习研究**。`images/` 中的游戏素材（干员头像、武器 / 护甲 / 大招图标、backdrop 等）
  版权归 **Riot Games** 所有，请勿用于商业用途或再分发盈利。
- 运行产物 `output/*.json` 含真实对局与玩家昵称，已由 `.gitignore` 排除。
- 本软件界面作者信息见应用内（B 站 @丶慕邵）。

## 许可证

代码部分以 **GPLv3** 授权，见 [LICENSE](LICENSE)。游戏素材版权归属 Riot Games，不在 GPL 范围内。
