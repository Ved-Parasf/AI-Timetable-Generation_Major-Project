GROQCLOUD UPGRADE (V18) — READ FIRST
===================================
The optional AI assistant has been upgraded from local Qwen2.5/Ollama to
GroqCloud GPT-OSS 120B. START WITH README_GROQ_CLOUD_UPGRADE.md for setup,
API-key steps, expected behavior, privacy notes and limitations.

The CP-SAT solver, fairness optimizer, four assistant features, SQLite
history, validated moves and highlighted revised timetables are preserved.
You do not need to install Ollama to use the cloud option.

AI based Timetable generation — Timetable Assistant update
===========================================================

This package contains the existing CP-SAT timetable generator, policies,
fairness reporting, independent validator, SQLite version history and a
four-option Timetable Assistant. The original Excel input is not rewritten.

RUN ON WINDOWS 10 (VS CODE / POWERSHELL)
----------------------------------------
1. Extract the ZIP to a NEW folder. Open that folder in VS Code; it contains
   timetable_app.py and requirements.txt.
2. If upgrading, STOP the old Streamlit app (Ctrl+C). Back up your previous
   data/coordinator_workflows.sqlite3 before making changes. To preserve the
   previous saved history, copy that SQLite file to the new project's data
   directory while both applications are stopped. Never overwrite it blindly.
3. Open Terminal > New Terminal (PowerShell) and run:

   py -m pip install -r requirements.txt
   py -m unittest discover -s tests -v
   py -m streamlit run timetable_app.py

   Alternatively, double-click start_timetable.bat AFTER dependencies are
   installed. Choose your Excel workbook in the Workspace and generate a
   timetable. The assistant requires a successfully generated, independently
   validated timetable.

THE FOUR ASSISTANT OPTIONS
--------------------------
1. Question Answering
   Ask natural-language questions about teachers, sections, courses, rooms,
   availability, workload and policies. For example:
       Which rooms are available on Tuesday?
   Whole-day available-room answers now list ALL matching room IDs per period,
   not just the first five. You can still request a short summary explicitly.

2. Find Free Slots
   Choose "A class" to see independently validated alternative periods and
   rooms for one selected teaching session. Choose "Available rooms" to get a
   COMPLETE scrollable table, filters for day/period/type/capacity and a CSV
   export. Room-only results do not establish that a class can be moved.

3. Shift a Class
   Enter a request such as:
       Shift MAT500 for DEMO-V-A to Thursday Period 2
   (Replace these example identifiers with actual ones from your workbook.)
   If several teaching sessions match, choose the exact one. Inspect legal
   alternatives, select one and tick the coordinator confirmation checkbox.
   Clicking "Confirm and update timetable" revalidates the COMPLETE changed
   timetable and atomically saves the original plus revised schedules in the
   local SQLite database, including the change audit. It rebuilds CSVs,
   timetable HTML and quality/validation reports, then refreshes Results.
   The moved session is highlighted with GOLD/AMBER and the RESCHEDULED tag
   in the section timetable. Two-period practicals remain continuous.
   Nothing changes simply because a user types "approve" in chat.

4. Check Conflicts
   Select the exact teaching session and proposed starting period. Check the
   requested slot against institutional rules, faculty/group availability,
   room constraints and independent post-solve validation. The checker can
   explain why it cannot recommend a slot, without editing any timetable.

SAVED VERSIONS / CSV
--------------------
- The original workbook remains the authoritative input. CSV/HTML are generated
  outputs. The local database is data/coordinator_workflows.sqlite3.
- Versions are immutable. Approved revisions are saved to the database while
  preserving the previous timetable. A saved historical version is read-only
  until opened as a working copy from Results > Saved runs.
- After approving a move, the application reloads the revised assignments and
  regenerates the report and CSV. It does NOT rerun the entire university-wide
  CP-SAT solver, because that would potentially discard the requested move.
- The old Results > Coordinator workflows request/approval flow remains available.
  It also highlights the moved session on approval.
- The SQLite coordinator role is for a local DEMO; there is no login, role-based
  authentication or real university account authorization. Do not deploy as a
  multi-user production approval system without security controls.

VALIDATION & LIMITS
-------------------
- These are safe single-session moves: other sessions remain fixed. A change
  requiring multiple dependent swaps is not supported.
- All suggestions use the existing candidate generator, policies and the
  independent full-timetable validator. If no valid schedule exists, the
  software does not invent one. GroqCloud GPT-OSS is OPTIONAL for understanding
  unusual queries, and cannot decide timetable facts or approve a move.
  A Groq API key and network access are required for cloud features.
- Five focused regression tests were executed successfully on the bundled
  compatible five-session university demo workbook: complete room listing,
  rescheduled HTML highlight, and independently validated SQLite revision plus
  saved working-copy reload. They are not proof of a full university solve.
- Streamlit and OR-Tools were not installed in the environment used to prepare
  this package, so the interactive browser UI and full 618-session generation
  must be verified on your Windows laptop.
- The university input may include synthetic facilities, faculty information
  and aggregate student memberships; independent validation does not verify
  real-world student-level elective conflicts.

IMPORTANT FILES
---------------
timetable_app.py                Streamlit interface and assistant dialog
timetable_assistant_features.py Four assistant modes and approved update
timetable_chat_moves.py         Natural-language move matching and previews
 timetable_rescheduling.py      Verified single-session move options
 timetable_workflows_ui.py      Report rebuilding / existing coordinator flow
 timetable_report.py            Highlighted section-wise HTML report
 timetable_records.py           SQLite saved versions and approvals
 timetable_versions.py          Working copies and archive selection
 timetable_validator.py         Independent post-solve checks
 northcap_cp_sat_solver.py       Existing CP-SAT timetable generator
 University_Timetable_Portable_Policy_Dataset.xlsx  Bundled prototype workbook
 Demo_University_B_Compatible_Dataset.xlsx         Small test workbook
