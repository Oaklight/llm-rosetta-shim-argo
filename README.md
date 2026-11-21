# llm-rosetta-shim-argonne

Provider shims for the three Argonne services, for
[llm-rosetta](https://github.com/Oaklight/llm-rosetta), in one plugin package:

| Vendor | Service | Shims |
|:---|:---|:---|
| `alcf` | ALCF inference endpoints (Argonne Leadership Computing Facility) | `alcf_metis--chat_completions`, `alcf_minerva--chat_completions`, `alcf_sophia--chat_completions` |
| `argo` | Argo, Argonne's internal LLM gateway | `argo--chat_completions`, `argo--messages`, `argo--open_responses` |
| `asksage` | AskSage (the ANL and public deployments) | `asksage--chat_completions`, `asksage--open_responses`, `asksage--messages`, `asksage--google_generate`, `asksage--embeddings` |

This repository was `llm-rosetta-shim-argo`. The `alcf` and `asksage` packages
(`llm-rosetta-shim-alcf`, `llm-rosetta-shim-asksage`) were merged into it with
their history (Oaklight/llm-rosetta#1660). Shim names did not change, so
existing provider entries keep working.

The package also ships `argo-tunnel`, the SSH tunnel CLI that makes Argo
reachable from outside the Argonne network; see
[The tunnel](#the-tunnel-argo-tunnel).

## Installing in the gateway

A gateway installs the vendors from this repository's **manifest**, one archive
per vendor, choosing for each vendor the newest version whose `requires` its
core satisfies, pinned by sha256:

```jsonc
{
  "shims": {
    "sources": ["argonne"],          // catalogue id, or the manifest URL below
    "install": ["argonne:*"]         // or single vendors: ["alcf", "argo"], or tags: ["tag:org:argonne"]
  }
}
```

The manifest lives on the orphan `dist` branch:

```
https://cdn.jsdelivr.net/gh/Oaklight/llm-rosetta-shim-argonne@dist/manifest.json
https://raw.githubusercontent.com/Oaklight/llm-rosetta-shim-argonne/dist/manifest.json   (fallback)
```

In a gateway that reads manifests, the older preset keys need no config edit:
`"presets": ["alcf", "argo", "asksage"]` maps to source `argonne` with
`install` set to the named vendors.

Reading manifests needs a gateway that implements `shims.sources`
(Oaklight/llm-rosetta#1660, step S2). A gateway before that loads the
repository tree from the default branch through its preset.

### As a Python package

```bash
pip install llm-rosetta llm-rosetta-shim-argonne
```

llm-rosetta discovers the package's three `llm_rosetta.shim_providers` entry
points (`alcf`, `argo`, `asksage`) during `import llm_rosetta` and registers
every shim. Core runs each entry point on its own, so a vendor that fails to
load is logged and the other two still register.

```python
from llm_rosetta.api import get_shim

shim = get_shim("argo--chat_completions")
print(shim.connection.base_url)
```

## Shims

### ALCF

The names follow core's `alcf_<cluster>--<format>` spelling: the cluster is in
the provider segment, and the segment after `--` is the API format. All three
clusters declare `group: alcf`, so the admin provider picker shows one `alcf`
group with the rows `metis · chat_completions`, `minerva · chat_completions`
and `sophia · chat_completions`.

| Shim | Format | Endpoint |
|:---|:---|:---|
| `alcf_metis--chat_completions` | `chat_completions` | `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1` |
| `alcf_minerva--chat_completions` | `chat_completions` | `https://inference-api.alcf.anl.gov/resource_server/minerva/api/v1` |
| `alcf_sophia--chat_completions` | `chat_completions` | `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1` |

Model IDs come from a shared `model_list_transform` (framework-aware filtering).

### Argo

| Shim | Format | Endpoint |
|:---|:---|:---|
| `argo--chat_completions` | `chat_completions` | `https://apps.inside.anl.gov/argoapi/v1` |
| `argo--messages` | `messages` | `https://apps.inside.anl.gov/argoapi` |
| `argo--open_responses` | `open_responses` | `https://apps.inside.anl.gov/argoapi` |

Each shim carries the Argo connection defaults, reasoning config, soft-error
patterns, and field and schema transforms (Gemini-via-Argo quirks, model-ID
mapping).

### AskSage

| Shim | Model type | Format |
|:---|:---|:---|
| `asksage--chat_completions` | `llm` | `chat_completions` |
| `asksage--open_responses` | `llm` | `open_responses` |
| `asksage--messages` | `llm` | `messages` |
| `asksage--google_generate` | `llm` | `google_generate` |
| `asksage--embeddings` | `embeddings` | `openai` |

`asksage--embeddings` targets `POST /server/openai/v1/embeddings`, under the
same `/server/openai/v1` root as the chat lane.

`asksage--messages` targets `/server/anthropic`. Measured 2026-11-06 against
the ANL deployment, that path answers HTTP 200 with an OpenAI
`chat.completion` body, not a Messages envelope. On ANL, use
`asksage--chat_completions` and let the gateway convert a client's Messages
request.

The `google_generate` leaf sends the token in `x-access-tokens`; the
`chat_completions` and `open_responses` leaves rename fields; the `embeddings`
leaf declares only its base URL and path.

The pre-0.14 spellings (`*--openai_chat`, `*--openai_responses`,
`*--anthropic`, `alcf--<cluster>`) do not resolve; use the names above.

## Credentials

Each vendor ships its credential exchange as `providers/<vendor>/auth.py`,
beside its vendor manifest, so every lane of a vendor authenticates alike.

### ALCF: a Globus grant

The ALCF inference service takes a Globus access token, minted from a grant
that a person obtains once by signing in at `auth.globus.org`. The first
sign-in is interactive, once per provider:

```bash
llm-rosetta credential login --provider "<the provider's name>"
```

It prints a Globus URL and takes the code that the page shows. In the admin
panel the same flow is the provider's **Sign in** action; the gateway host needs
no loopback port and no browser. After that, the rotating refresh token keeps
every cluster served: at startup, on the gateway's refresh interval, and after
an upstream 401.

The grant is kept where the vendor's own tools keep it,
`~/.globus/app/<client-id>/inference_app/tokens.json`, and written back in that
layout. An installation that already signed in with `alcf-ai` needs no step,
and a container that mounts `~/.globus` is configured once for both. Only when
that file does not exist is a record written under the gateway's `state_dir`.
`ALCF_API_KEY` is not read.

ALCF enforces a high-assurance session policy: a grant that keeps renewing can
still be refused once the interactive sign-in is too old, and the upstream 401
says so. Sign in again when that happens.

### Argo: the ANL account name, through the tunnel

Argo has no token to mint. The upstream checks the ANL account name, which goes
in the provider's `api_key` (or `ARGO_API_KEY`). The exchange declares
`consumes_key`, so the key field stays and is the only secret. When the
provider declares a SOCKS `proxy` and nothing listens on it, the exchange
reports "needs sign-in" with the `argo-tunnel auth` command instead of letting
the request fail with a 502. It never starts, stops or repairs the tunnel, and
it stores nothing.

| Panel state | Means |
|:---|:---|
| Connected | The account name is set and the tunnel answers |
| Needs sign-in | The proxy is declared but nothing listens on it |
| Failing | No account name is configured |

### AskSage: the account key, exchanged for a token

AskSage's inference lanes take a short-lived access token minted by
`POST {host}/user/get-token-with-api-key` from the account key that the holder
copies from the AskSage website.

- **The account key is the input.** Put it in the provider's `api_key`, or
  export `ASKSAGE_API_KEY`. The minted token goes upstream, never the key.
- **The host is part of the credential.** The token is minted against the
  origin of the provider's `base_url`, so the ANL and public deployments
  cannot be mixed up (the mismatch gave `Key doesnt exist`).
- **The account email is required.** In order of precedence: the **Account
  email** field on the provider form (stored in the entry's `auth_fields`), a
  flat `email` key, or `ASKSAGE_TOKEN_EMAIL`.

The token is cached until shortly before its `exp` claim.

An embeddings provider declared under `providers:` shares its LLM lane's key
ring, so one entry serves every lane with the same minted token:

```jsonc
{
  "providers": {
    "asksage": {
      "shim": "asksage--chat_completions",
      "api_key": "<account key>",
      "embeddings_shim": "asksage--embeddings"
    }
  },
  "embeddings_models": { "text-embedding-3-small": "asksage" }
}
```

A legacy `embeddings_providers` entry has no LLM lane to share the token with,
so an entry there that mints is disabled with a reason that names the fix:
move the provider to `providers:`.

## The tunnel (`argo-tunnel`)

`apps.inside.anl.gov` resolves only inside the Argonne network, and the jump
host asks for Duo MFA on every new connection. The `argo-tunnel` console
script owns that: one authenticated SSH master connection, one SOCKS5
forward on top of it, and a status you can branch on.

```bash
argo-tunnel auth pding --target lambda5.cels.anl.gov   # the one MFA prompt
argo-tunnel start                                      # publish the SOCKS forward
argo-tunnel proxy-url                                  # socks5://127.0.0.1:10801
```

No install needed — the same program is published as one file:

```bash
curl -fsSLO https://raw.githubusercontent.com/Oaklight/llm-rosetta-shim-argonne/main/scripts/argo-tunnel.py
python argo-tunnel.py auth pding --target lambda5.cels.anl.gov
```

`auth` takes the account name in ssh's own position, and everything else that
ssh takes: `--key/-i` for the private key, `--config/-F` for the config file,
`--jump/-J` for the hop. With an alias it inherits that stanza's user, key and
route, so `argo-tunnel auth --target lambda5` needs no other flag.

### Which machine it connects to

`--target` is an **ssh destination**, passed to ssh verbatim:

| `--target` | What ssh does |
|---|---|
| `lambda5` | Uses your `~/.ssh/config` `Host lambda5` stanza — its `HostName`, `Port`, `IdentityFile`, and `ProxyJump` if it has one |
| `me@lambda5.cels.anl.gov` | Connects to that, exactly as `ssh me@lambda5.cels.anl.gov` would |
| *(omitted)* | Uses the `ssh_config` alias that maps to `homes.cels.anl.gov` if there is one; otherwise builds `homes.cels.anl.gov` through `logins.cels.anl.gov` |

With a target, no `-J` is passed unless you give `--jump`: an alias normally
carries its own `ProxyJump`, and overriding it would silently replace your
routing with this tool's guess. `--jump none` means a direct connection.

`--user` is applied as the login prefix on an **explicit** target too, which is
how ssh itself composes `pding@lambda5`. A target that already carries a
different account (`--user someone --target other@lambda5`) is refused rather
than silently losing one of them. With no `--user`, an alias is left bare so the
stanza's own `User` applies.

When no target is given at all, a one-label `ssh_config` alias whose `HostName`
is the default host (`Host homes` → `homes.cels.anl.gov`) is preferred over the
bare name — that is the stanza that carries the account, the key and the route.

Then point the Argo provider's `proxy` field at that URL:

```jsonc
{
  "providers": {
    "argo": {
      "type": "argo--chat_completions",
      "proxy": "socks5://127.0.0.1:10801",
      "api_key": "<your ANL username>"
    }
  },
  "shims": { "sources": ["argonne"], "install": ["argo"] }
}
```

`base_url` stays `https://apps.inside.anl.gov/argoapi/v1` — a **dynamic**
(SOCKS) forward, not a local `-L` one, so the hostname the shim declares is the
hostname the TLS certificate is checked against. A local forward would answer
on `127.0.0.1` and fail verification.

### Commands

| Command | Does | Prompts |
|---|---|---|
| `auth` | Opens the master connection (`ssh -M -N -f`), then adds the forward; reuses a live master without prompting; rolls back a master it created if the forward fails | yes, once |
| `start` | Adds the forward to the running master (`ssh -O forward -D`); idempotent | no |
| `stop` | Cancels the forward; the master stays authenticated | no |
| `status` | Prints `forwarding` / `idle` / `wedged` / `absent`; exit code 0 only for `forwarding` | no |
| `logout` | Drops the master, and any forward on it | no |
| `proxy-url` | Prints the URL to paste into a provider config | no |

`status --json` prints the same state as one object, for a launcher:

```json
{"state": "forwarding", "proxy_url": "socks5://127.0.0.1:10801",
 "socket": "/home/user/.config/llm-rosetta-gateway/tunnel/ssh-1a2b3c4d5e6f",
 "target": "homes.cels.anl.gov", "jump": "pding@logins.cels.anl.gov"}
```

### Where the socket lives, and how long it lasts

Both come from your own SSH setup first. The CLI reads the effective
configuration with `ssh -G`, so whatever you set globally is what gets used:

| | Your `ssh_config` sets it | You pass a flag / env | Neither |
|---|---|---|---|
| socket | `ControlPath` — used as given | `--state-dir` / `ARGO_TUNNEL_STATE_DIR`, or `--socket` / `ARGO_TUNNEL_SOCKET` for an exact path | `$XDG_CONFIG_HOME/llm-rosetta-gateway/tunnel/ssh-<hash>` (else `~/.config/…`) |
| lifetime | `ControlPersist` — used as given | `--control-persist` / `ARGO_TUNNEL_CONTROL_PERSIST` | `8h` |

```bash
argo-tunnel status --json   # "socket" tells you which one is in play
```

Three details worth knowing:

- **A `ControlMaster auto` you already configured is reused, not routed
  around.** If `ssh_config` names a `ControlPath`, that is the socket — a tool
  that invented its own would report `absent` while an authenticated master sat
  right there.
- **`ControlPersist` counts from the last *use*, not the last authentication.**
  A request through the forward counts. Measured: with `ControlPersist=20s`
  and traffic at t=12s, the master exited at t=33s — the deadline moved with
  the traffic. Under steady use the master does not expire; an idle one does.
  A request that is still in flight when the master exits fails; the next
  request needs `start` again, or `auth` if the master is gone.
- **Socket paths have a hard length limit** (`sun_path`, ~100 bytes). A deep
  `$HOME` would silently truncate; when the path it would use is too long the
  CLI stops and asks for a shorter `--state-dir` rather than guessing one.
- **The state directory must be `0700` and is created that way.** A missing one
  is created on first use; an existing one with looser permissions is refused
  with the exact `chmod 700 <dir>` to run. It is never changed automatically —
  a directory that happens to hold other things is not this tool's to tighten.

`--ssh-config FILE` reads an additional ssh config file (ssh still reads
`~/.ssh/config` itself).

### When the port is taken

A bound `--port` is checked **before** `auth` runs, so a conflict costs no MFA
prompt. The message names the address and, when the OS will say, the process
holding it:

```
error: SOCKS port 127.0.0.1:10801 is already in use by ssh (pid=2654558)
hint: Stop that listener. If it is an SSH master this tool no longer tracks,
      kill its pid; otherwise pick another port with --port.
```

A listening SOCKS server is never taken as proof that the forward is this
tool's: `status` reports `forwarding` only for a forward it recorded, and
`wedged` when something else holds the port. `argo-tunnel` never kills a
listener and never removes a socket it did not create.

`auth` also confirms the port actually listens after `ssh -O forward` returns
success, and reports it if it does not.

Re-entering an existing tunnel is not a conflict. The record of which forward
belongs to which socket is keyed by the socket, and changing `--target` or
`--config` moves the socket while the forward stays where it is. In that case
`auth` asks the master to add the forward again — `ssh -O forward` is
idempotent — and answers `forward already up` instead of demanding a second
Duo approval.

`auth` takes an account name in ssh's position (`auth pding`). A subcommand
name there (`auth status`) is refused by name, because it would otherwise be
read as an account called `status` and fail as a login error.

`logout` closes only a master this tool can claim. The profile is the record,
but a live `ssh -M -S <our socket>` is the same fact read from the process
table (via `/proc`, Linux only), so a lost profile no longer leaves you unable
to close a master that `argo-tunnel` started.

### Configuration

Every command accepts `--target`, `--key`, `--config`, `--proxy`, `--host`,
`--jump`, `--user`, `--bind`, `--port`, `--control-persist` and `--state-dir`
before or after the subcommand, and reads the same names
from the environment as `ARGO_TUNNEL_HOST`, `ARGO_TUNNEL_JUMP`,
`ARGO_TUNNEL_USER`, `ARGO_TUNNEL_BIND`, `ARGO_TUNNEL_PORT`,
`ARGO_TUNNEL_SOCKET` and `ARGO_TUNNEL_CONTROL_PERSIST`. `ARGO_TUNNEL_JUMP=none`
drops the jump hop, for a host already inside the network.

The defaults are the Argonne pair: `homes.cels.anl.gov` through
`logins.cels.anl.gov`, port `10801` on `127.0.0.1`.

### Notes

- **Only `auth` prompts.** Everything else runs with `BatchMode=yes`, so a
  launcher or a unit file gets a failure instead of a hung process. If `auth`
  is run while a master is already up, it says so rather than asking for a
  second approval.
- **`wedged` is a real state.** A master that does not answer (as opposed to
  one that was never there) needs `logout` first; `start` refuses it rather
  than forwarding over a dead control socket.
- **Two goals need two masters.** The control socket path is derived from the
  target, the jump host and the port, so a second target does not silently
  reuse the first one's session.
- **Containers.** An image cannot see the host's control socket or its
  `127.0.0.1`. Bind the forward to the host's bridge address
  (`--bind 172.17.0.1`) and point `proxy` at it. Never bind `0.0.0.0`: SOCKS5
  here has no authentication.

## Repository layout

```
src/llm_rosetta_shim_argonne/
  __init__.py              entry points: register_alcf / register_argo / register_asksage
  tunnel.py                the argo-tunnel CLI (stdlib only)
  providers/
    shims.yaml             every shim the package ships
    alcf/                  provider.yaml (vendor manifest), auth.py, model_utils.py,
                           <cluster>/llm/{provider.yaml,transforms.py}
    argo/                  provider.yaml, auth.py, model_utils.py, llm/<format>/...
    asksage/               provider.yaml, auth.py, llm/<format>/..., embeddings/
tests/{alcf,argo,asksage}/ each vendor's tests
tests/test_layout.py       entry points, import boundary, archive loading
tests/test_manifest.py     the manifest builder
vendors.toml               per-vendor release metadata (version, maturity, tags)
scripts/build_shim_archive.py   per-vendor archives
scripts/build_manifest.py       archives + manifest.json
```

Shim code imports core only through `llm_rosetta.contracts` (sidecars and entry
points run during `import llm_rosetta`, when `llm_rosetta.api` cannot be
imported yet); tests may also use `llm_rosetta.api`. `tests/test_layout.py`
enforces the first rule.

## Releasing

A vendor's version, maturity and tags live in `vendors.toml`. A release:

1. Bump the version of each vendor whose files changed in `vendors.toml`, and
   the package `__version__` in `src/llm_rosetta_shim_argonne/__init__.py`.
   The tag must equal `__version__`.
2. `python scripts/build_manifest.py --dry-run` builds every archive, checks
   its layout, and prints the manifest. CI runs the same dry run and validates
   the result on every push.
3. Tag and push `v<__version__>`. `.github/workflows/release.yml` extends the
   published manifest with a record for each vendor whose archive changed,
   writes `dist/<version>/<vendor>.tar.gz` (and `.sha256`),
   `dist/manifest.json` and `dist/<release>/manifest.json` to the `dist`
   branch, and attaches them to the GitHub Release.

A published record is never edited. An unchanged archive keeps its record; an
archive whose bytes changed under an already-published version fails the
build until its version is bumped. The workflow needs `contents: write`.

## Requirements

- Python >= 3.10 (the release builder needs 3.11+ for `tomllib`)
- `llm-rosetta >= 0.14.0.dev35`, the first core with `llm_rosetta.contracts`.
  There is no fallback for older cores; the manifest's `requires` keeps a
  gateway on an older core from installing these archives.
- For the tunnel: `ssh` on `PATH`, and an SSH account that reaches
  `homes.cels.anl.gov` through `logins.cels.anl.gov`. The tunnel does not need
  llm-rosetta; importing the package does not import it.

## License

MIT; see [LICENSE](LICENSE).
