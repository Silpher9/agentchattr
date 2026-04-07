# Private Mobile Access via Tailscale

Access agentchattr from your phone over a private, encrypted connection using Tailscale.

## Why Tailscale?

Agentchattr runs on localhost by default. To use it from your phone, you need network access to the host machine. There are several ways to do this, but most are **unsafe**:

| Method | Security | Recommended? |
|---|---|---|
| **Tailscale** | Encrypted (WireGuard), authenticated, private | Yes |
| Port forwarding | Plaintext, open to the internet | No |
| ngrok / Cloudflare Tunnel | Public URL, anyone with the link has access | No |
| Open WiFi + 0.0.0.0 | Plaintext, anyone on the network can sniff tokens | No |

Tailscale creates a private network (tailnet) between your devices using WireGuard encryption. Only devices signed into your Tailscale account can reach each other. No ports are opened on your router.

**Threat model:**

| Threat | Mitigation |
|---|---|
| Unauthorized access | Tailscale: only authenticated devices in your tailnet |
| Man-in-the-middle | WireGuard end-to-end encryption |
| DNS rebinding | Origin check in agentchattr security middleware |
| Remote agent spoofing | Agent registration is loopback-only |
| Token sniffing | Traffic is encrypted within the Tailscale tunnel |

---

## Requirements

- agentchattr running on a Linux or macOS machine (the "host")
- A [Tailscale account](https://tailscale.com/) (free tier is sufficient)
- A mobile device (iOS or Android)
- Both devices signed into the same Tailscale account

---

## Step 1: Install Tailscale on the host

### Linux

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

### macOS

Install from the [Mac App Store](https://apps.apple.com/app/tailscale/id1475387142) or via Homebrew:

```bash
brew install tailscale
sudo tailscale up
```

### Verify

```bash
tailscale ip -4
```

This prints your Tailscale IP (e.g., `100.64.x.x`). Write this down — you'll need it in step 3.

You can also verify the connection:

```bash
tailscale status
```

You should see your machine listed as "online".

---

## Step 2: Install Tailscale on your phone

- **iOS**: Install [Tailscale](https://apps.apple.com/app/tailscale/id1470499037) from the App Store
- **Android**: Install [Tailscale](https://play.google.com/store/apps/details?id=com.tailscale.ipn) from the Play Store

Sign in with the same account you used on the host.

### Verify

Open the Tailscale app on your phone. You should see your host machine listed with a green "connected" status. Note the host's Tailscale IP shown in the app — it should match the IP from step 1.

---

## Step 3: Configure agentchattr for Tailscale access

Create a `config.local.toml` file in the agentchattr root directory (this file is gitignored):

```toml
[server]
host = "100.64.x.x"  # Replace with your Tailscale IP from step 1
```

This tells agentchattr to bind on your Tailscale IP instead of localhost.

> **Why config.local.toml?** This file is gitignored, so your personal network settings won't be committed to the repository. The main `config.toml` stays unchanged.

> **Why not 0.0.0.0?** Binding to `0.0.0.0` exposes agentchattr on *all* network interfaces, including your local WiFi. Binding to the Tailscale IP specifically ensures only Tailscale traffic can reach it.

---

## Step 4: Start agentchattr

```bash
python run.py --allow-network
```

You'll see a security warning and a confirmation prompt:

```
  !! SECURITY WARNING — binding to 100.64.x.x !!
  This exposes agentchattr to your local network.
  ...
  Type YES to accept these risks and start:
```

Type `YES` to confirm. This is expected — you're intentionally binding to your Tailscale interface.

### Verify

You should see output like:

```
  agentchattr
  Web UI:  http://100.64.x.x:8300
  MCP HTTP: http://100.64.x.x:8200/mcp
  ...
```

Confirm the Web UI URL shows your Tailscale IP.

---

## Step 5: Connect from your phone

1. Make sure Tailscale is connected on your phone (check the Tailscale app)
2. Open your mobile browser (Safari, Chrome, etc.)
3. Navigate to `http://<tailscale-ip>:8300` (e.g., `http://100.64.1.23:8300`)

### Verify

The agentchattr UI should load. You should see the chat interface with your channels and messages.

If you get a "connection refused" or timeout:
- Check that Tailscale is connected on both devices
- Check that the IP matches
- Check that agentchattr is running with the correct host

If you get a 403 "forbidden: origin not allowed":
- Verify that the host in `config.local.toml` matches exactly the IP you're accessing from the browser
- Restart agentchattr after changing the config

---

## Step 6: Add to home screen (optional)

### iOS (Safari)

1. Open agentchattr in Safari
2. Tap the **Share** button (square with arrow)
3. Scroll down and tap **Add to Home Screen**
4. Name it "agentchattr" and tap **Add**

### Android (Chrome)

1. Open agentchattr in Chrome
2. Tap the **three-dot menu** (top right)
3. Tap **Add to Home screen** (or **Install app**)
4. Confirm

This creates a home screen shortcut that opens agentchattr directly. Note: this is a simple shortcut, not a full PWA — there is no offline support or push notifications.

---

## Troubleshooting

### "Connection refused" or timeout

- **Tailscale not connected**: Open the Tailscale app on both devices and verify they show as connected
- **Wrong IP**: Run `tailscale ip -4` on the host and compare with the URL you're using
- **agentchattr not running**: Check that `python run.py --allow-network` is running on the host
- **Firewall**: Some Linux distributions block incoming connections by default. Check `sudo ufw status` or equivalent

### "forbidden: origin not allowed" (403)

- The IP in `config.local.toml` must match exactly the IP you access in the browser
- After changing `config.local.toml`, restart agentchattr
- Check that you're using `http://` (not `https://`)

### Agents not responding

- Agent wrappers (`wrapper.py`) run on the host and are not affected by Tailscale
- If agents were running before the config change, restart them so they pick up the new host
- Agent registration is loopback-only by design — agents must run on the same machine as the server

---

## What NOT to do

These configurations are **unsafe** and should not be used:

- **Do not** use port forwarding on your router to expose agentchattr to the internet
- **Do not** use ngrok, Cloudflare Tunnel, or similar services to create a public URL
- **Do not** bind to `0.0.0.0` unless you understand that this exposes agentchattr on all network interfaces (including local WiFi)
- **Do not** use this setup on public or shared WiFi networks
- **Do not** share your agentchattr URL or session token with others
- **Do not** disable the `--allow-network` security prompt

Agentchattr has no user authentication — anyone who can reach the server and obtain the session token has full access, including the ability to trigger agent tool execution.
