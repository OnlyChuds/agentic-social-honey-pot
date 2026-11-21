# OpenCode Setup — Qwen3.8-27B Abliterated (AMD Radeon Cloud)

Connect [OpenCode](https://opencode.ai) to our hosted **`Qwen3.8-27B-abliterated`** model (`65,536` context window, streaming, reasoning, and tool calling enabled).

> Ask a teammate for the shared API key — do **not** commit it to git.

---

## 1. Install OpenCode (if not already installed)

```bash
curl -fsSL https://opencode.ai/install | bash
```

---

## 2. Configure the Provider

Edit (or create) your global OpenCode config at **`~/.config/opencode/opencode.jsonc`** (using the global path ensures your API key is never accidentally committed to this repo):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "radeon-cloud": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AMD Radeon Cloud",
      "options": {
        "baseURL": "https://rc-a38ff61e5d582336.radeon.firstdg.ai/v1",
        "apiKey": "{env:RADEON_QWEN_API_KEY}"
      },
      "models": {
        "Qwen3.8-27B-abliterated": {
          "name": "Qwen3.8 27B Abliterated",
          "limit": {
            "context": 65536,
            "output": 16384
          }
        }
      }
    }
  },
  "model": "radeon-cloud/Qwen3.8-27B-abliterated"
}
```

> Alternatively, you can paste the API key directly into `"apiKey"` in `~/.config/opencode/opencode.jsonc` instead of using `{env:RADEON_QWEN_API_KEY}`.

---

## 3. Set the API Key & Launch OpenCode

Export the shared key (add this to `~/.bashrc` or `~/.zshrc` to persist across terminals):

```bash
export RADEON_QWEN_API_KEY="YOUR_API_KEY_HERE"
```

Then launch OpenCode:

```bash
opencode
```

If you keep another model as your default, you can either:
- Launch directly with `-m`:
  ```bash
  opencode -m radeon-cloud/Qwen3.8-27B-abliterated
  ```
- Or switch inside OpenCode using `/models` → **`radeon-cloud/Qwen3.8-27B-abliterated`**.

---

## 4. Verify the Connection

```bash
opencode models radeon-cloud
opencode run -m radeon-cloud/Qwen3.8-27B-abliterated "Reply with exactly: OK"
```

---

## Browser Chat UI

You can also chat directly in your browser at:

https://rc-a38ff61e5d582336.radeon.firstdg.ai/

Enter the shared API key when prompted to unlock the UI.
