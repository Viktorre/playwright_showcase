# Agentic RPA Browser Agent

An LLM-driven browser agent that accomplishes goals you describe in plain language.
Uses Groq (Llama 4 Scout) for decisions and Playwright for browser control.

Core loop: **observe DOM → LLM picks an action → Playwright executes → repeat**

---

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m playwright install chromium
```

Create a `.env` file:

```
GROQ_API_KEY=your-groq-key
HEADLESS=false
```

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Required. Get one at https://console.groq.com |
| `HEADLESS` | `false` = visible browser (local), `true` = headless (server) |
| `AGENT_MODEL` | Optional. Defaults to `meta-llama/llama-4-scout-17b-16e-instruct` |

---

## Usage

One-shot task:
```bash
./venv/bin/python agent.py "search trains from Berlin to Munich on bahn.de"
```

Interactive chat (also accepts browser tasks):
```bash
./venv/bin/python agent.py
```

---

## EC2 Deployment

Infrastructure is managed with Terraform in the `terraform/` folder.

### Deploy
```bash
cd terraform
terraform init
terraform apply
```

### Connect

Copy `EC2_PUBLIC_IP` from `.env`, then:
```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@35.158.27.184
```

### Destroy
```bash
cd terraform
terraform destroy
```

### Server setup (after first deploy)

SSH in (see Connect above), then on the server:
```bash
git clone https://github.com/Viktorre/playwright_showcase.git
cd playwright_showcase
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m playwright install --with-deps chromium
```

Then create `.env` on the server with `HEADLESS=true` and your `GROQ_API_KEY`.

### Scheduled runs (every 12 hours)
```bash
crontab -e
# Add:
0 */12 * * * cd /home/ubuntu/playwright_showcase && ./venv/bin/python agent.py "your task here" >> agent.log 2>&1
```

---

## Files

| File | Purpose |
|------|---------|
| `agent.py` | The agent: DOM observation, tool declarations, the loop |
| `.env` | Secrets and config (gitignored) |
| `requirements.txt` | Python dependencies |
| `terraform/` | EC2 infrastructure-as-code |
