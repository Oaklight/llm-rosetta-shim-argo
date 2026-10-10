# llm-rosetta-shim-argo

Argo (Argonne) provider shims for [llm-rosetta](https://github.com/Oaklight/llm-rosetta),
delivered as a **plugin package** rather than as built-in shims.

This is the standalone-repository variant of
[llm-rosetta#666](https://github.com/Oaklight/llm-rosetta/issues/666): it verifies
the plugin shim loader ([#272](https://github.com/Oaklight/llm-rosetta/issues/272) /
[#273](https://github.com/Oaklight/llm-rosetta/issues/273)) against a real,
provider-specific shim set. The built-in `argo--*` shims in llm-rosetta are left
in place for now; once this package is stable they can be removed from core.

## Shims

| Shim | Base | Endpoint |
|:---|:---|:---|
| `argo--openai_chat` | `openai_chat` | `https://apps.inside.anl.gov/argoapi/v1` |
| `argo--anthropic` | `anthropic` | `https://apps.inside.anl.gov/argoapi` |
| `argo--openai_responses` | `openai_responses` | `https://apps.inside.anl.gov/argoapi` |

Each shim carries the Argo-specific connection defaults, reasoning config, soft-error
patterns, and field/schema transforms (Gemini-via-Argo quirks, model-ID mapping).

## Install

```bash
pip install llm-rosetta
pip install llm-rosetta-shim-argo
```

llm-rosetta discovers the `llm_rosetta.shim_providers` entry point on startup and
registers the `argo--*` shims automatically — no import needed in application code.

## Usage

```python
from llm_rosetta.shims import get_shim

shim = get_shim("argo--openai_chat")     # requires ARGO_API_KEY in the environment
print(shim.connection.base_url)
```

## Requirements

- Python >= 3.10
- `llm-rosetta >= 0.13.0` (needs the plugin shim loader)
- `ARGO_API_KEY` in the environment at request time

## License

MIT — see [LICENSE](LICENSE).
