---
name: feishu-model-picker
description: >
  飞书（Lark）机器人自定义菜单 → Hermes 交互式卡片切换模型。用户点飞书菜单项
  触发 /model pick <provider>，Hermes 回复带模型按钮的卡片，点按钮即刻切换当前
  profile 的 model+provider（is_global=True 持久化到 config.yaml，无需重启
  gateway 生效）。多 profile 支持：每个 gateway 进程独立读取自己 HERMES_HOME 的
  config.yaml。
license: MIT
---

# Feishu Model Picker — 飞书菜单驱动的模型切换

给飞书机器人加**两级菜单**式的模型切换：主菜单显示 provider，点开后弹出飞书交互式卡片，卡片上是该 provider 的所有模型按钮。点按钮即刻切换到目标模型并写回 `config.yaml`。

> 英文说明见 `README.md`。本文件是 Hermes skill 形式的使用指南，便于 `skill_view` 直接查看。

## 组件

| 类型 | 路径 | 作用 |
|---|---|---|
| 插件 | `$HERMES_HOME/plugins/feishu-model-picker/__init__.py` | 执行逻辑（hook + 卡片构建 + switch_model 调用）|
| 配置 | `plugin.yaml` | 插件元数据 |
| 文档 | `docs/feishu-menu-setup.md` | 飞书后台菜单配置指南 |
| 可选 | `display_names.example.json` | 模型显示名别名示例 |

## 安装

用随包的 `install.sh`（幂等）把插件装进当前 profile：

```bash
./install.sh
```

或手动复制：

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
cp -r feishu-model-picker "${HERMES_HOME:-$HOME/.hermes}/plugins/"
```

### 1. 各 profile 启用插件

`config.yaml` 里加：

```yaml
plugins:
  enabled:
    - feishu-model-picker
```

或用 CLI：

```bash
hermes config set plugins.enabled '["feishu-model-picker"]'
```

多 profile 环境每个 profile 各执行一次（`HERMES_HOME` 指向对应 profile）：

```bash
HERMES_HOME=/path/to/profiles/<name> hermes config set plugins.enabled '["feishu-model-picker"]'
```

### 2. 让 profile 能看到插件

如果插件放在共享位置而各 profile 扫不到，为每个 profile 建 symlink：

```bash
ln -sf "$HERMES_HOME/plugins/feishu-model-picker" \
  /path/to/profiles/<name>/plugins/feishu-model-picker
```

### 3. 配置飞书机器人自定义菜单

在 [飞书开发者后台](https://open.feishu.cn/app) → 你的应用 → 应用能力 → 机器人 → **机器人自定义菜单**，启用并添加菜单项，响应动作选 **发送文字消息**，内容填 `/model pick <provider>`（`<provider>` 换成你 `custom_providers[]` 里的 provider 名）。

详细步骤见 `docs/feishu-menu-setup.md`。

### 4. 重启 gateway 加载插件

```bash
hermes gateway restart
```

多 profile 各重启一次。`.py` 代码改动必须重启 gateway 才生效；只有 `display_names.json` 是热加载的。

## 使用

1. 打开和 bot 的**单聊**（菜单不支持群聊）。
2. 点菜单项（例如「🎯 myprovider」）→ bot 回复带模型按钮的卡片。
3. 点一个模型按钮 → bot 立即切换，回复确认卡片。
4. 也可直接发 `更换模型` / `切换模型` / `/model` 打开**全部 provider** 的汇总卡片。

## 支持的 provider

插件从 `config.yaml` 的 `custom_providers[]` 读取模型列表，支持其中定义的任何自定义 provider。provider slug → emoji 的映射在 `__init__.py` 的 `_PROVIDER_EMOJI`，友好名在 `_PROVIDER_DISPLAY_NAMES`，都可自行扩展；未命中走默认 🔌。

## 模型名称显示

按钮文字默认**就是完整原始模型 ID**，不缩写、不截断。想启用别名显示，改 `_display_name()` 让它查 `_reload_external_names()`，并把别名写进 `$HERMES_HOME/plugins/feishu-model-picker/display_names.json`（可从 `display_names.example.json` 复制），或设环境变量 `FEISHU_PICKER_MODEL_DISPLAY_NAMES`（JSON）。文件改动热加载，无需重启。

## 工作原理（架构）

```
飞书菜单点击 → 发送文字 "/model pick <provider>"
    ↓
Hermes gateway 收到消息 → pre_gateway_dispatch hook 触发
    ↓
插件匹配 /model pick 正则 → 构建 provider 卡片 → 发送 → 返回 {"action":"skip"}
    ↓
用户点模型按钮 → 飞书 POST p2_card_action_trigger
    ↓
Feishu adapter._handle_card_action_event 合成 "/card switch_model {json}"
    ↓
pre_gateway_dispatch hook 拦截 → 识别 mp_switch=switch_model
    ↓
asyncio.run_coroutine_threadsafe 调度到 gateway loop
    ↓
hermes_cli.model_switch.switch_model(is_global=True) → 写 config.yaml → 发 ack 卡片
```

## 排错

**菜单点击没反应**：飞书应用版本已发布（等约 5 分钟）；客户端 ≥ 7.22（发送文字消息）；是单聊；gateway 日志应有 `[ModelPicker] Sent provider card for ...`。

**卡片发出但点按钮没反应**：gateway 日志应有 `[ModelPicker] Switching -> <model> on <provider>`；常见失败是 `config.yaml` 里没有该 provider 的 `custom_providers[]` 定义。

**切换失败**：看 gateway 日志的 `[ModelPicker]` 行；常见 `Unknown provider 'custom:xxx'` → 检查 `custom_providers` 的 provider 名。

## 相关文件

- 插件代码：`feishu-model-picker/__init__.py`
- 飞书卡片文档：https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-interactive-components
