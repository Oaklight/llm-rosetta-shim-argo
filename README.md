# llm-rosetta-shim-argo

Argo (Argonne) provider shims for [llm-rosetta](https://github.com/Oaklight/llm-rosetta),
delivered as a **plugin package** rather than as built-in shims.

The `argo--*` shims left llm-rosetta core in 0.14.0
([#666](https://github.com/Oaklight/llm-rosetta/issues/666)); this package is now
their only source. Declare it in the gateway config and existing provider
entries keep their names:

```jsonc
{ "shims": { "presets": ["argo"] } }
```

`shims.presets` expands to this repository's archive path via llm-rosetta's
catalogue; the equivalent explicit entry is `{"url": "...", "path":
"src/llm_rosetta_shim_argo/providers"}`. The repository is public, so no
`token_env` is needed.

## Shims

| Shim | Base | Endpoint |
|:---|:---|:---|
| `argo--chat_completions` | `chat_completions` | `https://apps.inside.anl.gov/argoapi/v1` |
| `argo--messages` | `messages` | `https://apps.inside.anl.gov/argoapi` |
| `argo--open_responses` | `open_responses` | `https://apps.inside.anl.gov/argoapi` |

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

shim = get_shim("argo--chat_completions")   # requires ARGO_API_KEY in the environment
print(shim.connection.base_url)
```

The pre-0.14 spellings `argo--openai_chat`, `argo--openai_responses` and
`argo--anthropic` no longer resolve to a shim: this package registers the
canonical names only. They remain in llm-rosetta as a spelling map, so an old
config canonicalises first and reaches the error naming this package. Update
the spelling to the canonical name above.

## Archive and mirror

Every release publishes one archive of the bundled shims, so a gateway can load
them from a host other than GitHub. `main` never carries it: `dist/` is build
output and is in `.gitignore`.

- `v<version>/argo.tar.gz` and `v<version>/argo.tar.gz.sha256` live on the
  orphan `dist` branch.
- The same bytes are attached to the GitHub Release, with the digest in its
  notes.

Two interchangeable, version-pinned URLs for the same bytes:

```
https://github.com/Oaklight/llm-rosetta-shim-argo/releases/download/v<version>/argo.tar.gz
https://cdn.jsdelivr.net/gh/Oaklight/llm-rosetta-shim-argo@dist/v<version>/argo.tar.gz
```

The archive's root is `providers/`, the layout the package ships under
`src/llm_rosetta_shim_argo/providers`, so a gateway source declares `path: "providers"`:

```jsonc
{
  "shims": {
    "remotes": [
      {
        "url": "https://cdn.jsdelivr.net/gh/Oaklight/llm-rosetta-shim-argo@dist/v<version>/argo.tar.gz",
        "path": "providers",
        "archive": "tar.gz",
        "sha256": "<digest from v<version>/argo.tar.gz.sha256>"
      }
    ]
  }
}
```

The digest is what pins the mirror, and the same value appears in the release
notes. jsDelivr is only the default suggestion: the gateway accepts any URL that
serves the same bytes, an internal mirror included.

### Publishing a release

1. Bump `__version__` in `src/llm_rosetta_shim_argo/__init__.py`. The tag must match it —
   the release workflow passes the tag without its `v` as `--expect-version` and
   refuses a mismatch.
2. `python scripts/build_shim_archive.py --write` builds
   `dist/v<version>/argo.tar.gz` and its digest locally and verifies the
   layout. CI runs the same build without writing on every push, so a tree whose
   `providers/` layout changed fails before a tag is cut.
3. Tag and push: `git tag v<version> && git push origin v<version>`.
4. `.github/workflows/release.yml` builds from the tag, creates or updates the
   GitHub Release, uploads the archive and its digest file, and commits
   `v<version>/` to the `dist` branch.

The workflow requests `contents: write` for `GITHUB_TOKEN`, because it creates
the release and pushes `dist`. An organization that downgrades action tokens to
read-only must restore "Read and write permissions" under Settings → Actions →
General, or the release step fails.

## Requirements

- Python >= 3.10
- `llm-rosetta >= 0.14.0.dev28` (needs the plugin shim loader and the
  vendor manifest)
- `ARGO_API_KEY` in the environment at request time

## License

MIT — see [LICENSE](LICENSE).
