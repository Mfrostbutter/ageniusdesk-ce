# Getting Started

AgeniusDesk CE is a self-hosted control plane for your n8n instances. The first run secures the dashboard with an owner account, then walks you through connecting (or standing up) n8n so the rest of the dashboard has something to manage. This page covers everything from the first browser visit to a connected instance.

## Install and open

If you have not started the app yet, the short version (full reference in the project `README.md`):

```bash
git clone https://github.com/Mfrostbutter/ageniusdesk-ce.git
cd ageniusdesk-ce
cp .env.example .env
docker compose up -d --build
```

Then open the dashboard:

```
http://localhost:3000
```

## Step 1: Create the owner account

On the very first visit, AgeniusDesk has no accounts, so it shows a full-screen **Create your owner account** form before anything else loads. This account secures your install; you sign in with it every time.

Fill in:

| Field | Notes |
|---|---|
| Email | This is your login identity and the address used for password reset. Must be a valid email shape (`you@example.com`). |
| Display name | Optional. |
| Password | Must meet the policy below. |
| Confirm password | Must match. |

As you type the password, a live checklist shows which requirements are met. The default password policy is:

| Requirement | Default |
|---|---|
| Minimum length | 12 characters |
| Uppercase letter | required |
| Lowercase letter | required |
| Number | required |
| Symbol | required |

(The policy is enforced server-side in `backend/modules/auth/service.py` / `router.py` and is configurable via the `AGD_PASSWORD_*` environment variables; the checklist reflects whatever your install is set to.)

Click **Create account**. The dashboard creates the account, signs you in, and reloads. Creating the owner account also resets any leftover onboarding state in the browser, so you always get the full walkthrough.

Note: this form only appears when no account exists yet. If your install runs behind a trusted proxy with `AGD_DISABLE_LOGIN=true` or edge identity, the gate is skipped entirely.

## Step 2: Signing in

On later visits you get the **Sign in** screen. Enter your email and password and click **Sign in**.

- **Forgot password?** opens the reset flow. Enter your account email and a reset link is sent (the response is always the same whether or not the email matches an account, so it never reveals which addresses exist). If SMTP is not configured, the reset link is written to the server log instead.
- If two-factor is enabled on your account, after the password step you are asked for the **6-digit code** from your authenticator app. You can enter a recovery code here instead if you have lost your device.

## Step 3: The setup wizard

Right after the owner account is created (whenever no n8n instances are configured), the setup wizard opens as a modal. The first screen, **Welcome**, asks how you want to get started. Every step after Welcome can be skipped.

| Path | What it does |
|---|---|
| **Stand up my stack on this host** (recommended) | One-click deploy of n8n, Infisical, and other services straight into the Docker daemon running the dashboard. Best for a fresh self-host. |
| **I already have n8n running** | You provide the URL and an API key. |
| **Walk me through self-hosting** | Shows quick-start guides (Docker Compose, DigitalOcean, Hostinger, Railway) on the Connect n8n step. |
| **n8n Cloud account** (beta) | Use your n8n.cloud workspace URL and an API key from the n8n Cloud UI. Not yet fully tested. |

The wizard steps adapt to the path you pick. The step strip at the top shows only the steps that apply.

### Stand-up-stack path

Steps: **Welcome -> Stand Up Stack -> Secrets -> AI Assistant -> Done**.

1. On **Stand Up Stack**, pick the services to deploy. Each one runs in the local Docker daemon. n8n and Infisical are pre-selected because they cover the headline case (automation plus secure credential storage). Each selected service shows an **Instance name** and **Host port** you can change, plus its other fields; passwords are auto-generated and shown after deploy. The host-port field warns inline if the port is already used by a running container, is browser-unsafe, or clashes with another service in the same stack, so you can fix it before deploying rather than after a failed bind.
2. Click **Deploy stack**. The wizard pulls images and starts each container in turn, showing per-service progress. A failure in one service (commonly a host-port clash) does not stop the others; you can use **Retry failed** inline after freeing the port, or finish those later from the Containers view.
3. When the deploy finishes, each ready service is listed with an **Open in browser** link. Click **Next** to continue to Secrets and AI Assistant.
4. The stack path cannot register the newly-deployed n8n inside the wizard, because n8n needs its own first-run owner account and API key first. That is handled by the connect guide after the wizard (Step 4 below).

You can also click **Skip and connect existing n8n** to abandon the stack deploy and fall through to the manual connect path instead.

### Have-n8n / walk-through / cloud paths

Steps: **Welcome -> Secrets -> Connect n8n -> AI Assistant -> Done**.

1. **Secrets** (optional). Store API keys in one encrypted place and reference them anywhere as `$NAME`. The form pre-populates blank rows for `ANTHROPIC_KEY`, `OPEN_AI_KEY`, and `OPEN_ROUTER_KEY`; fill in what you have, add more with **+ Add another**, and leave the rest blank. Values are saved with Fernet encryption. See [Secrets](secrets.md).
2. **Connect n8n**. Enter an **Instance Name**, the **n8n URL** (the URL you use to open n8n in your browser), and an **API Key**. On the walk-through path, the left side shows platform guides with per-OS install steps. To get an n8n API key: in n8n, go to **Settings -> n8n API -> Create an API key**. Click **Test connection** to validate without saving, then **Connect & Continue** to register the instance. The key is promoted into the encrypted secrets store as a `$REF` rather than stored in plaintext.
3. **AI Assistant** (optional). Pick a provider (OpenRouter, OpenAI, Anthropic, or Ollama), paste a key (or an Ollama URL), optionally choose a model, and **Test connection**. This unlocks in-dashboard chat, error diagnosis, and AI-assisted Code Lab. You can change it later in Settings.
4. **Done** summarizes what was configured. Click **Enter Dashboard**.

## Step 4: The "connect your n8n" guide

If you used the stand-up-stack path, the dashboard pops a guided **Connect your n8n** modal a moment after it loads. n8n is running but still needs its one-time setup before AgeniusDesk can talk to it. The guide walks three steps:

1. **Open n8n and create your account.** A button opens the deployed n8n in a new tab. n8n shows its own "set up owner account" screen on first run; this n8n login is separate from your AgeniusDesk login.
2. **Create an API key** in n8n at **Settings -> n8n API -> Create an API key**. Copy it (you only see it once).
3. **Register it here.** The form is pre-filled with a smart URL (the same host you reached AgeniusDesk by, with n8n's port). Paste the key and click **Connect**.

Tip from the guide: if the connection fails, use the machine's **LAN IP** (for example `http://192.168.x.x:5678`) rather than `localhost`. The dashboard runs in Docker, so `localhost` inside the container is not your host. (When you do enter a localhost URL, the backend transparently rewrites it to `host.docker.internal`; see [n8n Instances](instances.md).)

**I'll do this later** dismisses the guide; the dashboard's empty-state prompt still covers you. After a successful connect, the dashboard offers to wire up error reporting into n8n.

## The Setup Journey "Get started" card

On the Dashboard, a **Get started** card tracks onboarding milestones. It is not a stored step counter; each milestone is derived live from real app state on every load (see `frontend/js/onboarding/journey.js`), so it is always honest and resumable. The card shows a "Setup X of Y" progress count for the required milestones, auto-hides once the required path is complete, and can be reopened from Settings.

| Milestone | How it is marked done | Required? |
|---|---|---|
| Connect or stand up n8n | An instance is configured (`/api/n8n/instances` non-empty, or `/api/status` reports configured) | Yes |
| Turn on two-factor | TOTP enabled on your account; hidden entirely if login is disabled / edge-managed | Recommended |
| Add your provider keys | At least one secret exists in the store | Recommended |
| Configure the AI assistant | At least one assistant area has a provider and model set | Recommended |
| Meet the harness | You have visited the Knowledge view at least once | Recommended |

Each incomplete milestone has a button that jumps you to the right place (open the wizard, go to Settings, open Secrets, and so on). The **x** dismisses the card.

## Optional: turn on two-factor

For a shared or public deployment, enable two-factor from **Settings -> Account**. You enroll with an authenticator app, confirm a code to activate, and are given one-time recovery codes; store them somewhere safe. Once enabled, sign-in adds the 6-digit code step described in Step 2.
