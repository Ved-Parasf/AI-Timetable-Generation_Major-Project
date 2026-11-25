"""Audit a *generated* timetable CSV against open-elective policies P02–P05.

Usage:
  py open_elective_audit.py --excel University_Timetable_Portable_Policy_Dataset.xlsx --csv generated_timetable/timetable_all.csv

Checks prohibited classes according to the workbook's explicit Open Elective
mode and allowlist. Not a substitute for the full independent validator.
"""
from __future__ import annotations
import argparse
import csv
from pathlib import Path
from northcap_cp_sat_solver import excel_records
from timetable_policies import load_policies


def find_open_elective_violations(data, rows):
    from timetable_policies import check_elective_reservation
    policies = load_policies(data)
    if policies.errors:
        raise ValueError('Policy parsing failed: ' + '; '.join(policies.errors))
    return policies, check_elective_reservation(data, rows, policies)


def main():
    parser=argparse.ArgumentParser(description='Check P02-P05 compliance in generated timetable CSV')
    parser.add_argument('--excel', required=True, type=Path)
    parser.add_argument('--csv', required=True, type=Path)
    a=parser.parse_args()
    data=excel_records(a.excel)
    with a.csv.open('r', encoding='utf-8-sig', newline='') as handle:
        rows=list(csv.DictReader(handle))
    policies, violations=find_open_elective_violations(data,rows)
    print('Open-elective reserved slots:', ', '.join(sorted(policies.reserved_slot_ids)))
    print('Open Elective mode:', policies.elective_window_mode)
    print('Allowed course IDs:', ', '.join(sorted(policies.window_allowed_course_codes)) or '(none)')
    print('Forbidden session-period rows:', len(violations))
    for v in violations[:50]:
        print(f"  {v['error']}: {v.get('course_id')} / {v.get('group_id')} "
              f"{v.get('day')} Period {v.get('period')} (faculty {v.get('faculty_name', '')})")
    if len(violations)>50:
        print('  ...',len(violations)-50,'additional violating rows not printed')
    return 1 if violations else 0


if __name__=='__main__':
    raise SystemExit(main())
