<div align="center">

# MeerkatX

### Unified defense for every agent — ship AI apps that survive contact.

**Built by Team ExtraMile**

</div>

---

## What this is

AI-built applications can look complete and still ship with holes a read-through
misses. MeerkatX runs independent security engines against a target application
and merges what they find into one clear call: ship, or don't.

It's a front end over a multi-agent, LLM-driven security testing engine — you
give it a target, it dynamically exercises the running application (not just a
static read of the source), and validates what it finds with real
proof-of-concept exploits rather than flagging guesses.

**What gets tested:**

- **Application Security** — can attackers break the app? Authentication,
  secrets, SQL/command injection, misconfiguration.
- **AI Manipulation Testing** — can users manipulate the AI? Indirect prompt
  injection, unsafe tool invocation, data-theft actions, physical/financial harm.
- **Behavioral Validation** — can the AI stay useful and secure? Stateful tool
  use, legitimate task completion, attacker-goal detection, deterministic scoring.

## Try it

A hosted instance is running at:

**https://meerkatx-demo.eastus.cloudapp.azure.com**

Sign up, paste a target URL you're authorized to test, and run a scan. Results
land in your dashboard with cost, findings, and a chat assistant grounded in
that scan's own report.

## Running it yourself

Prerequisites: [Docker](https://docs.docker.com/get-docker/) (running) and an
LLM API key from any OpenAI-compatible provider.

```bash
# Clone and set up the engine
git clone https://github.com/mervinjosephl-hub/strix-agent-security.git
cd strix-agent-security
uv sync

# Configure your LLM provider
cat > .env <<EOF
STRIX_LLM=openai/gpt-5-mini
LLM_API_KEY=your-api-key
EOF

# Launch the web UI
uv run --with streamlit --with certifi streamlit run streamlit_ui/app.py
```

The UI opens at `http://localhost:8501` — sign up, launch a scan, watch it run
live, and browse results and history from the dashboard.

### Command-line usage

The underlying engine is also fully usable headless, without the UI:

```bash
# Scan a local codebase
uv run strix --target ./app-directory

# Black-box web application assessment
uv run strix --target https://your-app.com

# Non-interactive, for scripts/CI — exits non-zero if vulnerabilities are found
uv run strix -n --target https://your-app.com
```

See [`AGENTS.md`](AGENTS.md) for the full CLI reference.

## Architecture

- **`streamlit_ui/`** — the MeerkatX web front end: per-user auth, scan
  launcher, live progress, results/history dashboard, and a report-grounded
  chatbot.
- **`strix/`** — the underlying multi-agent scanning engine: reconnaissance,
  exploitation, and validation agents that run against the target in a sandboxed
  environment and write structured, evidenced findings.
- Deployment: a single Azure VM running the UI behind nginx/Let's Encrypt, with
  a GitHub Actions pipeline that redeploys on every push (see
  `.github/workflows/deploy-vm.yml`).

## Configuration

```bash
export STRIX_LLM="openai/gpt-5-mini"
export LLM_API_KEY="your-api-key"

# Optional
export LLM_API_BASE="your-api-base-url"   # for a custom/self-hosted endpoint
export STRIX_REASONING_EFFORT="high"      # thinking effort (default: high)
```

## Contributing

Issues and pull requests are welcome — this started as a hackathon build and
is still actively evolving.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

---

> [!WARNING]
> **Authorized use only.** MeerkatX actively tests the targets you point it
> at, so only run it against systems you own or have **explicit, written
> permission** to test, and stay within the agreed scope. Unauthorized testing
> is illegal in most jurisdictions. You alone are responsible for obtaining
> authorization and complying with the law. Provided "as is" with no warranty
> or liability for misuse.
