# Groq HTTP 403 fix: Python HTTP client signature

## Why the earlier fix did not work

A successful PowerShell call to `/chat/completions` proves the key/model worked for that specific request.
The previous Streamlit app also called `/chat/completions`, but used `urllib.request` without setting a
`User-Agent`. A documented Cloudflare rule can reject `Python-urllib` with 403/1010.
This update includes an explicit `TimetableAssistant/18.2` user agent in **both** the connection test
and normal Groq requests. The status display also distinguishes a Cloudflare 1010 rejection
from actual organization/project model permissions if Groq provides an error code.

No scheduling logic, policies, existing SQLite records or saved timetable files were changed.

## Install

1. Stop the old Streamlit app with `Ctrl+C`.
2. Back up your current project folder and `data/coordinator_workflows.sqlite3`.
3. Extract the new full project, or replace only `timetable_cloud_ai.py`.
4. From the project directory run:
   `py -m unittest discover -s tests -v`
   `py -m streamlit run timetable_app.py`
5. Paste exactly the same Groq key that succeeded in the PowerShell test.
6. Click `Connect Groq / Test key`.

If a 403 persists, compare the pasted key with the working PowerShell key,
and share only the displayed *error category* (never the key).

Testing is mocked: this build cannot call your Groq account.
