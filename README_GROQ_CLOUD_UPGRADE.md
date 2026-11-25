# Timetable Assistant — GroqCloud GPT-OSS 120B upgrade

This builds on the existing V17 project **AI based Timetable generation**. It replaces the optional local Ollama/Qwen integration with a hosted Groq API client. The CP-SAT timetable generator, fairness optimizer, workbook policies, independent validator, complete room searches, CSV output, coordinator approval, SQLite versions and rescheduled highlights are unchanged.

## Expected behavior

- **AI Q&A:** When Groq is connected, use the cloud model as a *structured, read-only intent planner* for unfamiliar natural-language questions. Python still calculates actual answers from the selected timetable and the original Excel workbook. Common supported questions may be answered locally without calling the API.
- **Conflicts:** The Check Conflicts option still uses the independent rescheduler. After checking, press **Explain these checked conflicts with cloud AI** for an optional plain-English explanation of the actual detected reasons. No change is made.
- **Fairness:** Results > Quality & fairness displays actual numerical metrics. Click **Explain measured fairness with cloud AI** to explain those figures and any existing Standard-versus-Fairness comparison. No metrics are invented; the AI narrative is supplementary.
- **Rescheduling:** The Shift a Class option still uses full independent validation and explicit coordinator confirmation. The stronger LLM does **not** itself move classes. Approved changes are saved as original and revised SQLite versions, updated in CSV and highlighted in the refreshed HTML timetable.
- **No API key, limited quota or no Internet:** Verified Python questions and scheduling workflows still work. Optional cloud explanations are unavailable until the connection succeeds.

A larger cloud model may interpret unfamiliar prompts more accurately, but this is not a guarantee. For reliable measurement compare a fixed set of questions before and after upgrading. Full NorthCap timetable feasibility is unrelated to model size.

## Windows setup (VS Code PowerShell)

1. **Backup your existing project folder**, especially `data\coordinator_workflows.sqlite3` if present. Do not merge or replace the database while Streamlit is running.
2. Extract this ZIP into a **new** folder. Open **Timetable_Assistant_Groq_Cloud** (the folder containing `timetable_app.py`) in VS Code.
3. Create a free GroqCloud account and API key at https://console.groq.com/keys (availability and free-tier quotas are controlled by Groq, and may change). **Never paste the key into ChatGPT or commit it to Git.**
4. In VS Code's PowerShell terminal:

```powershell
py -m pip install -r requirements.txt
py -m unittest discover -s tests -v
py -m streamlit run timetable_app.py
```

5. Upload the Excel input and generate or reopen an **independently validated** timetable. Go to **Results > Open Timetable Assistant > Question Answering**.
6. Turn on **Use GroqCloud AI understanding**. Choose `openai/gpt-oss-120b`, paste your API key into the **password** field, then click **Check Groq connection**. This key lives only in your running Streamlit session unless you separately configure environment variables/secrets.
7. Try a faculty query, a follow-up, **Check Conflicts > Explain these checked conflicts with cloud AI**, and **Results > Quality & fairness > Explain measured fairness with cloud AI**.

You may instead configure `GROQ_API_KEY` as a local environment variable or Streamlit secret. A `.streamlit/secrets.toml.example` guide is included; never put a real key in distributed project files.

### Privacy

Groq is an **external cloud service**. The read-only planner transmits the current question and a short previous question, **not** the entire timetable, teacher roster or workbook. Explanation buttons deliberately transmit a brief summary of checked diagnostics or numeric metrics. Questions themselves may contain teacher names or other identifiable data. Use synthetic/authorized inputs and follow university data-sharing requirements.

### Model, quotas, limitations

The default model is `openai/gpt-oss-120b`. `openai/gpt-oss-20b` is also available as a smaller alternative in the dropdown. Free-tier access, limits and supported models are controlled by Groq and are not guaranteed indefinitely. Internet access and an API key are required for cloud AI. No paid account or Ollama installation is required for the baseline project; an account may be needed to receive a free API key.

The application deliberately **does not** send a full schedule to the model or allow AI-generated commands to update scheduling policies, teacher assignments or database records. Conflict/rescheduling facts come from Python and CP-SAT; all approved modifications undergo the same independent validation as before.

### Technical implementation

- `timetable_cloud_ai.py`: HTTPS Groq chat completions, strict JSON read-only tool routing, API credential check and evidence-limited explanations. Uses only Python standard library (no additional package required).
- `timetable_nl_router.py` / `timetable_agent.py`: retain existing interfaces but use Groq, with Python fallback and local identity/slot matching.
- `timetable_app.py`: cloud model controls and fairness explanation button.
- `timetable_assistant_features.py`: conflict explanation button; approved class changes unchanged.
- `tests/test_groq_cloud_ai.py`: mocked API tests; no real network calls.

## Verification performed

Python syntax and all automated tests in this provided package were checked without a live API key. Live Groq connection, real model quality, full university-scale CP-SAT solves, and browser rendering need testing on the user's Windows machine.
