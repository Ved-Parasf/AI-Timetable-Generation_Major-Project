# Timetable Assistant V18 — Groq connection field fix

This is a targeted connection-setup fix. The CP-SAT solver, fairness optimization, Excel policies, class-move approval, SQLite history, and rescheduled-class highlighting are unchanged.

## What was wrong

- The API-key password field disappeared after Streamlit detected a value on rerun, so entering the key seemed to do nothing.
- A key previously set in Windows or `.streamlit/secrets.toml` took precedence over the key pasted in the app.
- Groq HTTP 401 and 403 errors were both shown as a vague key rejection.

## What changed

- The password field **always remains visible** in the Groq settings, with an explicit **Connect Groq / Test key** form submission button.
- The key typed into the application takes precedence over saved credentials. No API key is placed into project source, logs, or exported files.
- Connection results are shown on the page and persist until the model or key changes.
- An invalid key (401) and account/model permission issue (403) receive separate messages.
- The cloud AI enable toggle is independent from the connection test; leave it on to use the connected model.

## How to use

1. Stop the currently running Streamlit server with `Ctrl+C`.
2. Extract this package to a **new directory**, then open the inner `Timetable_Assistant_Groq_Cloud` directory in VS Code.
3. Back up your existing `data/coordinator_workflows.sqlite3` before switching versions if you have saved timetables. Keep the old folder intact.
4. In VS Code PowerShell, run:

   ```powershell
   py -m pip install -r requirements.txt
   py -m unittest discover -s tests -v
   py -m streamlit run timetable_app.py
   ```

5. In Results, open Timetable Assistant. Expand **Cloud AI settings (Groq)** (open by default).
6. Paste your GroqCloud API key from <https://console.groq.com/keys> into **Paste GroqCloud API key**.
7. Click **Connect Groq / Test key**. Typing alone does **not** initiate a network request.
8. Look for a green success message. If it fails, the message distinguishes invalid credentials, access permissions, rate limits, and network errors.
9. Turn **Use GroqCloud AI understanding** on to enable cloud assistance. The Python/CP-SAT features still work if cloud AI cannot connect.

**Security:** Never share your Groq API key or post screenshots showing it. The field masks it, and the project does not intentionally write it to disk. For university data, follow your institution's cloud sharing requirements.

## Verification limits

Local regression tests use mocked credentials and network responses. This package was not authenticated against a real Groq account or tested in an interactive Windows Streamlit browser. Actual connection depends on your key, provider permissions, and Internet access.
