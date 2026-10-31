# PiKVM

Drives a PiKVM's screen, keyboard, mouse and ATX power over kvmd's REST API.
A PiKVM is an **appliance**: it runs no Rook worker. Onboard it once (the hub
finds it by its `/api/info` answering `kvmd`), give it its web password, and
a worker that can reach it carries its calls. Name the PiKVM in the call; the
caps don't take a host.

| Cap | What it does |
|---|---|
| `kvm.screen.capture` | The captured screen as a JPEG, full size; its pixels are `hid.click`'s `x`/`y`. `preview=true` is kvmd's small thumbnail (0.4's default). |
| `kvm.hid.type` | Types text as a keyboard would (newlines press Enter). `enter=true` presses Enter after it; `slow=true` for machines that drop keys. |
| `kvm.hid.key` | One key with modifiers held: `key=c, mods=[ctrl]`; `key=delete, mods=[ctrl, alt]`. |
| `kvm.hid.move` | Pointer to an absolute position in kvmd units (-32768..32767, 0 is the centre). |
| `kvm.hid.click` | Clicks `left`, `right` or `middle`. With `x`, `y` it first moves to that pixel of the captured screen. |
| `kvm.power.on` | Short ATX power press (does nothing when the machine is on). |
| `kvm.power.off` | Short press (the OS shuts down); `force=true` holds the button. The hub asks first. |
| `kvm.power.reset` | ATX reset. The hub asks first. |
| `kvm.power.status` | Power and disk LEDs, and whether a press is in progress. |
| `kvm.api_get` / `kvm.api_post` | Any `/api/` endpoint, for what the caps above don't cover. `api_post` can do anything the PiKVM can, so the hub asks first. |

The PiKVM also answers the shared device caps: `screen.capture`, `hid.type`,
`hid.key`, `hid.click` and `power.off` sent to it reach the caps above.

Key names for `hid.key`: enter, escape, tab, backspace, space, delete, insert,
home, end, pageup, pagedown, up, down, left, right, printscreen, menu,
f1..f12, a..z, 0..9. Any kvmd key name (`KeyA`, `NumpadEnter`, `AltRight`)
also works as is.

## Instance settings

- `user`: the PiKVM web user (default `admin`).
- `password`: kept in the vault, fetched per call.
- `verify_tls`: check the certificate (off by default: PiKVMs ship a
  self-signed one).

The host name may carry a port (`kvm.lan:8443`).

The 0.4 `pikvm.*` names are gone (0.5 is a clean cut). `pikvm.power` (one cap with an `action`) is now
`power.on` / `power.off` / `power.reset`.
