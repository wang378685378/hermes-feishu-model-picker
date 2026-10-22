# Feishu Model Picker — a Hermes Agent plugin

A two-tier model picker for **Feishu (Lark)** bot custom menus. Switch the
model your Hermes agent uses by tapping buttons in a chat — no commands to
memorize, no config editing.

- Tap a bot menu item → it sends `/model pick <provider>` as text.
- The plugin intercepts it (via the `pre_gateway_dispatch` hook) and replies
  with an interactive Feishu card listing that provider's models as buttons.
- Tap a model button → the plugin switches the active `model` + `provider`,
  persists it to `config.yaml` (`is_global=True`, no gateway restart needed),
  and posts an acknowledgement card.
- Send `更换模型` / `切换模型` / `/model` to open a single card grouping every
  configured provider and its models.
- Multi-profile safe: each profile's gateway process reads its own
  `HERMES_HOME/config.yaml`.

> Zero third-party dependencies — standard library + `hermes_cli` only.
> Model listing/switching uses the host Hermes install's own `hermes_cli`.

## Requirements

- A working [Hermes Agent](https://hermes-agent.nousresearch.com/docs) install
  with the Feishu/Lark platform connected.
- Python 3.9+ (the gateway's interpreter).
- One or more providers defined under `custom_providers[]` in `config.yaml`.

## Install

```bash
./install.sh
```

This copies the `feishu-model-picker/` directory into
`${HERMES_HOME:-$HOME/.hermes}/plugins/` and verifies the layout. It is
idempotent — safe to re-run to upgrade.

Then enable the plugin for the profile and restart its gateway:

```bash
hermes config set plugins.enabled '["feishu-model-picker"]'
hermes gateway restart
```

For a multi-profile setup, repeat with `HERMES_HOME` pointing at each profile.
Full details (menu setup, symlinks, troubleshooting) are in
`feishu-model-picker/SKILL.md` and `feishu-model-picker/docs/feishu-menu-setup.md`.

## Configure the Feishu menu

In the [Feishu developer console](https://open.feishu.cn/app) →
your app → Bot → **Custom Menu**, add menu items whose action is
**send a text message** with content `/model pick <provider>`, where
`<provider>` matches a `name`/`provider` from your `custom_providers[]`.
Step-by-step guide: `feishu-model-picker/docs/feishu-menu-setup.md`.

## Customize

- `_PROVIDER_EMOJI` — emoji per provider slug (fallback 🔌).
- `_PROVIDER_DISPLAY_NAMES` — friendly provider labels on the all-providers card.
- `_CHINESE_ALIASES` — menu-label aliases → commands (`/mp <provider>` or a
  built-in slash command like `/stop`).
- Model button labels show the **raw model ID** by default. To use aliases,
  edit `_display_name()` to consult `_reload_external_names()`, then put your
  map in `$HERMES_HOME/plugins/feishu-model-picker/display_names.json`
  (template: `display_names.example.json`) or set the
  `FEISHU_PICKER_MODEL_DISPLAY_NAMES` env var. The JSON file hot-reloads;
  `.py` changes require a gateway restart.

## How it works

```
Feishu menu tap → text "/model pick <provider>"
  → gateway receives → pre_gateway_dispatch hook fires
  → plugin matches, builds provider card, sends it, returns {"action":"skip"}
Tap a model button → Feishu posts p2_card_action_trigger
  → adapter synthesizes "/card switch_model {json}"
  → hook intercepts → schedules switch_model(is_global=True) on gateway loop
  → writes config.yaml → sends ack card
```

## License

MIT — see `LICENSE`.
