"""Timora Workspace — multi-page Streamlit interface for the existing CP-SAT solver.

Floating coordinator chat + more complete grounded answers. The original CP-SAT
solver, independent validator and dataset are preserved.
Provisional flags remain in CSV/report data without visual 'Assumed' badges.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from zipfile import ZIP_DEFLATED, ZipFile

import streamlit as st

st.set_page_config(
    page_title="Timora Workspace | University Timetable Generator",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Navigation and file bytes live outside widgets, so returning to the welcome
# page doesn't erase the uploaded dataset or an already-generated timetable.
for name, value in {
    "studio_page": "home",
    "workbook_bytes": None,
    "workbook_name": None,
    "workbook_digest": None,
    "university_name": "",
    "solver_result": None,
    "standard_result": None,
    "standard_workbook_digest": None,
    "upload_version": 0,
    "assistant_history": [],
    "chat_move_state": None,
    "assistant_move_reply": "",
    "coordinator_preview": None,
    "coordinator_last_diagnosis": "",
    "agent_use_llm": True,
    "agent_model": "openai/gpt-oss-120b",
}.items():
    if name not in st.session_state:
        st.session_state[name] = value


STYLES = r"""
<style>
:root{--navy:#111d3e;--ink:#152445;--muted:#64748f;--blue:#4b66eb;--teal:#20b6aa;--line:#e3eaf4;--paper:#fff;--bg:#f5f7fc}
.stApp{background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}
.block-container{max-width:1320px;padding-top:1.05rem;padding-bottom:4.8rem}
header[data-testid="stHeader"]{background:transparent}
#MainMenu,footer{visibility:hidden}
.stButton>button{border-radius:12px;transition:all .18s ease;font-weight:700;min-height:43px;border-color:#dae4f2;color:#334a70;background:#fff}
.stButton>button:hover{border-color:#7892df;color:#2c50be;background:#f2f6ff;transform:translateY(-1px)}
.stButton>button[kind="primary"]{color:#fff;background:linear-gradient(115deg,#4b63e8,#357aca);border:0;box-shadow:0 9px 22px #4b63e82b;min-height:48px}
.stButton>button[kind="primary"]:hover{color:#fff;background:linear-gradient(115deg,#3d54d7,#2d6dbb);box-shadow:0 12px 24px #4968ed40}
.stButton>button:disabled{transform:none;box-shadow:none}
.stDownloadButton>button{border-radius:11px;min-height:46px;background:#fff;border:1px solid #dce5f2;color:#2b4b7d;font-weight:700}
.stDownloadButton>button:hover{background:#f0f5ff;color:#264fbe;border-color:#c6d7f4}
[data-testid="stFileUploader"]{background:#fff;border:1px solid #e0e8f4;border-radius:18px;padding:19px 22px;box-shadow:0 11px 32px #21395d0a}
section[data-testid="stFileUploaderDropzone"]{border:1.5px dashed #b3c6e7;background:#f7faff;border-radius:13px}
[data-testid="stExpander"]{border:1px solid #e1e8f3!important;background:#fff;border-radius:13px!important;overflow:hidden}
[data-testid="stMetric"]{border-radius:15px;border:1px solid #e2eaf5;background:#fff;padding:17px 19px;box-shadow:0 9px 22px #162c4b09}
[data-testid="stMetricValue"]{font-weight:800;letter-spacing:-1px;color:var(--navy)}
[data-testid="stMetricLabel"]{font-weight:700;color:#627594}
[data-testid="stTabs"] [data-baseweb="tab-list"]{gap:8px;border-bottom:1px solid #e3e9f5}
[data-testid="stTabs"] button[role="tab"]{font-weight:700;border-radius:10px 10px 0 0;color:#53698d}
.app-wordmark{display:flex;align-items:center;gap:10px;padding:4px 0 12px}
.app-symbol{width:40px;height:40px;display:grid;place-items:center;border-radius:13px;background:linear-gradient(135deg,#38c6bc,#5677fa);color:white;font-weight:900;font-size:21px;box-shadow:0 9px 17px #4e82f533}
.app-wordmark b{font-size:17px;letter-spacing:-.55px;color:#16284e}
.app-wordmark small{font-size:10px;color:#8a97ae;display:block;letter-spacing:1px;text-transform:uppercase;font-weight:700;margin-top:1px}
.micro-label{color:#6079d8;font-size:11px;letter-spacing:2px;text-transform:uppercase;font-weight:850}
.home-hero{font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;display:grid;grid-template-columns:1.18fr .82fr;min-height:420px;border-radius:26px;overflow:hidden;background:linear-gradient(115deg,#111c39 0%,#162854 58%,#194c73 100%);color:#fff;box-shadow:0 22px 53px #10274425;margin:16px 0 12px}
.home-copy{padding:52px 48px;align-self:center;z-index:1}
.home-eyebrow{display:inline-block;border:1px solid #ffffff35;border-radius:30px;background:#ffffff15;padding:8px 13px;font-size:10.5px;letter-spacing:1.7px;font-weight:850;color:#8af0dd}
.home-hero h1{font-size:clamp(37px,4.35vw,58px);line-height:1.09;letter-spacing:-2px;margin:23px 0 19px;font-weight:850}
.home-hero h1 em{font-style:normal;color:#80eddc}
.home-hero p{font-size:15px;line-height:1.78;color:#cad8ef;max-width:570px}
.hero-mini{display:flex;gap:10px;flex-wrap:wrap;margin-top:24px}
.hero-mini span{font-size:11px;border:1px solid #ffffff30;color:#d6e7fa;border-radius:30px;padding:7px 12px;background:#ffffff12}
.home-art{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;padding:36px 27px}
.home-art:before{content:"";position:absolute;border-radius:50%;width:470px;height:470px;right:-130px;top:-130px;border:1px solid #ffffff2c;box-shadow:0 0 0 62px #ffffff09,0 0 0 132px #ffffff05}
.calendar-demo{position:relative;background:#ffffffed;border:1px solid #ffffffa8;border-radius:20px;padding:19px 18px;width:min(100%,375px);box-shadow:0 22px 55px #0e1b3580;transform:rotate(-3deg)}
.demo-top{display:flex;align-items:center;justify-content:space-between;color:#172a4d;margin-bottom:16px}
.demo-top b{font-size:15px;letter-spacing:-.5px}.demo-top small{font-size:10px;color:#71829b}
.demo-week{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}
.demo-day{text-align:center;color:#7789a5;font-size:10px;font-weight:800;margin-bottom:7px}
.demo-cell{min-height:44px;background:#eff3fa;border-radius:7px;padding:4px;position:relative}
.demo-pill{height:28px;background:#dbe6ff;border-left:3px solid #5979df;border-radius:5px;margin-bottom:5px}
.demo-pill.green{background:#d8f5eb;border-color:#28bd9c}.demo-pill.gold{background:#ffefcc;border-color:#e6ab3f}.demo-pill.purple{background:#eae4ff;border-color:#967ad9}
.demo-cap{margin-top:12px;color:#8fa0b7;text-align:center;font-size:9px;letter-spacing:.4px}
.hero-cta-note{color:#7e8da5;font-size:12px;align-self:center;padding-top:10px}
.value-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:17px;margin:23px 0 34px}
.value-card{border:1px solid #e4eaf4;background:#fff;border-radius:18px;padding:24px;box-shadow:0 9px 27px #1a315008}
.value-icon{width:43px;height:43px;border-radius:13px;background:#eaf0ff;display:grid;place-items:center;color:#3e60ca;font-weight:900;font-size:18px}
.value-card h3{margin:17px 0 8px;color:#17294b;font-size:17px;letter-spacing:-.4px}.value-card p{font-size:12.5px;color:#7e8ba0;line-height:1.7;margin:0}
.section-eyebrow{font-size:11px;color:#506bce;letter-spacing:1.65px;font-weight:850;text-transform:uppercase}
.section-title-large{font-size:clamp(25px,3vw,34px);line-height:1.2;color:#152746;letter-spacing:-.9px;font-weight:850;margin:8px 0 10px}
.section-subtitle{color:#72839c;font-size:14px;line-height:1.7;margin:0 0 23px;max-width:810px}
.how-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px;margin-top:19px}
.how-card{padding:19px;border-radius:14px;background:#f0f4fb;border:1px solid #e1e9f5}
.how-card span{font-weight:900;color:#4268d9;font-size:12px;letter-spacing:1px}.how-card b{display:block;font-size:14px;margin:9px 0 6px;color:#24375a}.how-card small{font-size:12px;line-height:1.6;color:#7385a2}
.workspace-banner{border-radius:21px;background:linear-gradient(110deg,#111e40,#1b3968);padding:31px 35px;color:white;margin:13px 0 21px;position:relative;overflow:hidden}
.workspace-banner:after{content:"";position:absolute;right:-30px;top:-100px;border:1px solid #ffffff21;border-radius:50%;width:340px;height:340px;box-shadow:0 0 0 47px #ffffff08}
.workspace-banner .section-eyebrow{color:#87e5d7;position:relative}.workspace-banner h1{font-size:33px;letter-spacing:-1.1px;margin:10px 0 7px;position:relative}
.workspace-banner p{color:#cad9f0;font-size:13px;margin:0;position:relative}
.steps-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0 23px}
.steps-item{display:flex;align-items:center;gap:13px;border:1px solid #e3eaf5;background:white;border-radius:13px;padding:14px 15px}
.steps-num{display:grid;place-items:center;flex-shrink:0;height:33px;width:33px;color:#486bd9;background:#eaf1ff;border-radius:10px;font-size:11px;font-weight:900}
.steps-item b{font-size:12px;color:#263d63;display:block}.steps-item small{display:block;color:#8b99ad;font-size:11px;margin-top:3px}
.note-card{background:#edf7f5;border:1px solid #d3ebe5;color:#327967;padding:13px 16px;border-radius:13px;font-size:12.5px;line-height:1.55;margin:12px 0 16px}
.notice-card{color:#697a93;background:#f0f3f9;border:1px solid #e0e8f4;padding:12px 15px;font-size:12.5px;line-height:1.6;border-radius:11px;margin:17px 0}
.result-banner{padding:22px 27px;border-radius:17px;border:1px solid #d6e9e7;background:linear-gradient(105deg,#e8f9f2,#eaf3ff);margin:10px 0 19px}
.result-banner h2{font-size:22px;color:#1c4a50;letter-spacing:-.6px;margin:0 0 6px}.result-banner p{font-size:12px;color:#51767a;margin:0}
.error-banner{padding:20px 23px;border-radius:14px;background:#fff0ed;border:1px solid #f4c6bd;color:#994c47;margin:18px 0}
.result-preview{border:1px solid #dce7f5;background:#fff;box-shadow:0 15px 34px #182e520e;border-radius:17px;overflow:hidden}
/* Read-only verified analytics — dashboard visual language. */
.analytics-hero{position:relative;overflow:hidden;padding:32px 34px;margin:3px 0 18px;
  border-radius:20px;background:linear-gradient(110deg,#152548 0%,#234a76 66%,#177b86 100%);
  color:white;box-shadow:0 13px 32px #18375c27}
.analytics-hero:after{content:"";position:absolute;right:-65px;top:-98px;width:320px;height:320px;
  border:1px solid #ffffff36;border-radius:50%;box-shadow:0 0 0 52px #ffffff09,0 0 0 108px #ffffff06;pointer-events:none}
.analytics-eyebrow{font-size:10.5px;font-weight:850;letter-spacing:2px;color:#8df4df}
.analytics-hero h2{color:white;position:relative;font-weight:850;letter-spacing:-.9px;font-size:clamp(23px,3vw,33px);
  margin:10px 0 8px}
.analytics-hero p{position:relative;color:#d4e5f3;margin:0;line-height:1.65;max-width:750px;font-size:13px}
@media(max-width:690px){.analytics-hero{padding:24px 21px}.analytics-hero p{font-size:12px}}

.site-footer{border-top:1px solid #e1e8f3;padding:20px 0 0;margin-top:38px;display:flex;justify-content:space-between;gap:15px;color:#8795aa;font-size:11px}
@media(max-width:950px){.home-hero{grid-template-columns:1fr}.home-copy{padding:35px 26px 18px}.home-art{min-height:240px;padding:10px 30px 30px}.calendar-demo{transform:rotate(-2deg)}.value-grid,.how-grid{grid-template-columns:1fr}.steps-strip{grid-template-columns:1fr}}
@media(max-width:700px){.block-container{padding-top:.6rem}.home-hero h1{font-size:35px}.home-copy{padding:30px 24px 18px}.workspace-banner{padding:24px}.workspace-banner h1{font-size:26px}.site-footer{display:block}}

/* Interface accessibility: consistent light-control contrast regardless of OS theme. */
:root{color-scheme:light}
.block-container{max-width:1510px}
[data-testid="stFileUploader"] label,
[data-testid="stFileUploader"] label p,
[data-testid="stFileUploader"] [data-testid="stMarkdownContainer"] p,
[data-testid="stFileUploaderDropzone"] [data-testid="stMarkdownContainer"],
[data-testid="stFileUploaderDropzone"] small,
[data-testid="stFileUploaderDropzone"] span{
  color:#243b60!important;
}
[data-testid="stFileUploaderDropzone"]{
  background:#f6f9ff!important;
  color:#243b60!important;
  border:1.5px dashed #a8bee4!important;
}
[data-testid="stFileUploaderDropzone"] button,
[data-testid="stFileUploaderDropzone"] button[kind],
[data-testid="stFileUploaderDropzone"] button span{
  color:#264eaf!important;
  background:#e8efff!important;
  border-color:#bed0ee!important;
  font-weight:750!important;
}
[data-testid="stFileUploaderDropzone"] button:hover,
[data-testid="stFileUploaderDropzone"] button:hover span{
  color:#133c99!important;
  background:#dbe7ff!important;
}
[data-testid="stFileUploader"] [data-testid="stFileUploaderFile"]{
  background:#edf3ff!important;
  color:#203b67!important;
  border-radius:9px;
}
[data-testid="stFileUploader"] [data-testid="stFileUploaderFile"] *{
  color:#203b67!important;
}
[data-testid="stExpander"],
[data-testid="stExpander"] details,
[data-testid="stExpander"] [data-testid="stExpanderDetails"]{
  background:#fff!important;
  color:#1c3559!important;
}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary:hover{
  background:#f6f9ff!important;
  color:#213b63!important;
}
[data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary span,
[data-testid="stExpander"] summary svg{
  color:#203b63!important;
  fill:none;
}
[data-testid="stExpander"] label,
[data-testid="stExpander"] label p,
[data-testid="stExpander"] [data-testid="stMarkdownContainer"] p{
  color:#294061!important;
}
/* Chat input is in a local form, never the global browser-bottom chat bar. */
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea{
  color:#152b4e!important;
  background:#fff!important;
  -webkit-text-fill-color:#152b4e!important;
  caret-color:#4665e8!important;
  border-color:#ccd8ec!important;
}
[data-testid="stTextInput"] input::placeholder{
  color:#788ca9!important;
  -webkit-text-fill-color:#788ca9!important;
  opacity:1!important;
}
[data-testid="stTextInput"] label,
[data-testid="stTextInput"] label p{
  color:#263f68!important;
}
[data-testid="stChatMessage"]{
  background:#f1f5fd!important;
  border:1px solid #e0e8f5!important;
  border-radius:13px!important;
  padding:12px!important;
  margin-bottom:8px!important;
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"],
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] *,
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li{
  color:#1d3357!important;
  -webkit-text-fill-color:#1d3357!important;
}
[data-testid="stChatMessage"] strong{font-weight:750}
/* Floating action button: remains on screen while the report scrolls. */
.st-key-floating_assistant_launcher{
  position:fixed!important;
  bottom:25px;
  right:28px;
  z-index:999;
  width:max-content!important;
  padding:0!important;
  margin:0!important;
  filter:drop-shadow(0 13px 25px rgba(30,60,121,.27));
}
.st-key-floating_assistant_launcher [data-testid="stButton"] button{
  background:linear-gradient(125deg,#3857da,#19a99b)!important;
  border:1px solid #ffffff60!important;
  border-radius:100px!important;
  padding:11px 21px!important;
  min-height:56px!important;
  min-width:267px!important;
  color:#fff!important;
  font-weight:800!important;
  font-size:14px!important;
  box-shadow:inset 0 1px 0 #ffffff44,0 12px 30px #2744a633!important;
  letter-spacing:.1px;
}
.st-key-floating_assistant_launcher [data-testid="stButton"] button:hover{
  transform:translateY(-3px)!important;
  background:linear-gradient(125deg,#2746c7,#0c988c)!important;
  box-shadow:0 14px 33px #2042bd44!important;
}
/* Streamlit's native modal is used for the conversation, not a scrolling page column. */
[data-testid="stDialog"] [role="dialog"]{
  border:1px solid #dce7f7!important;
  border-radius:22px!important;
  box-shadow:0 25px 80px #10224133!important;
  background:#f9fbff!important;
}
[data-testid="stDialog"] [data-testid="stChatMessage"]{
  background:#eef3ff!important;
  border-color:#dce6f9!important;
}
[data-testid="stDialog"] [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]){
  background:#e7f2f0!important;
  border-color:#cdebe7!important;
}
[data-testid="stDialog"] [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] label p{color:#20385d!important;}
[data-testid="stDialog"] [data-testid="stForm"]{
  border-radius:13px!important;
  background:#fff!important;
  border:1px solid #e3e9f4!important;
  padding:12px!important;
}
@media(max-width:690px){
  .st-key-floating_assistant_launcher{right:12px;bottom:14px}
  .st-key-floating_assistant_launcher [data-testid="stButton"] button{min-width:230px!important;padding:9px 13px!important;font-size:12px!important}
}


.chat-shell{display:flex;flex-direction:column;gap:12px;padding:4px 2px 10px}
.chat-row{display:flex;width:100%}
.chat-row.user{justify-content:flex-end}
.chat-row.assistant{justify-content:flex-start}
.chat-bubble{max-width:88%;padding:14px 16px;border-radius:18px;border:1px solid #dee7f5;box-shadow:0 10px 22px rgba(19,39,75,.05)}
.chat-row.user .chat-bubble{background:linear-gradient(135deg,#edf7f4,#f7fbff);border-color:#d7e8e4;border-bottom-right-radius:8px}
.chat-row.assistant .chat-bubble{background:#ffffff;border-color:#dfe7f3;border-bottom-left-radius:8px}
.chat-author{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:#5f74a0;margin-bottom:8px}
.chat-row.user .chat-author{color:#1c7f78}
.chat-text{font-size:13.25px;line-height:1.72;color:#203559;white-space:pre-wrap;word-wrap:break-word}
.chat-subtle{font-size:12px;color:#6c7e97}
.chat-sticky-form{border:1px solid #dde6f4;background:#fff;border-radius:18px;padding:14px 14px 4px;box-shadow:0 16px 36px rgba(16,34,64,.06)}
.archive-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0 6px}
.archive-card{background:#fff;border:1px solid #e1e8f3;border-radius:16px;padding:16px 18px;box-shadow:0 10px 24px rgba(16,34,64,.04)}
.archive-card b{display:block;font-size:12px;color:#627594;margin-bottom:10px}.archive-card span{display:block;font-size:27px;font-weight:850;color:#152746;letter-spacing:-1px}
.premium-inline-note{background:linear-gradient(110deg,#f5f9ff,#eef8f7);border:1px solid #dbe7f2;border-radius:14px;padding:14px 16px;color:#49627d;font-size:12.5px;line-height:1.65;margin:12px 0 18px}
[data-testid="stDialog"] [role="dialog"]{max-width:920px!important}
[data-testid="stDialog"] .block-container{padding-top:.85rem!important;padding-bottom:1rem!important}


/* Native Streamlit-markdown chat bubbles: preserve emphasis, lists and line breaks. */
[data-testid="stDialog"] [class*="st-key-concierge_user_"],
[data-testid="stDialog"] [class*="st-key-concierge_assistant_"]{
  width:88%;max-width:88%;padding:13px 18px!important;
  margin:7px 0 13px 0!important;border:1px solid #dfe8f4!important;
  border-radius:19px!important;background:#fff!important;
  box-shadow:0 9px 26px rgba(20,42,80,.05)!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_user_"]{
  margin-left:auto!important;margin-right:0!important;
  background:linear-gradient(135deg,#eaf6f4,#f7fafc)!important;
  border-color:#cce7e3!important;border-bottom-right-radius:8px!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_assistant_"]{
  margin-left:0!important;margin-right:auto!important;
  border-bottom-left-radius:8px!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_"] [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] [class*="st-key-concierge_"] [data-testid="stMarkdownContainer"] li{
  font-size:14px!important;line-height:1.58!important;color:#233653!important;
  overflow-wrap:anywhere!important;white-space:normal!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_"] [data-testid="stMarkdownContainer"] p{
  margin:.25rem 0 .6rem!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_"] [data-testid="stMarkdownContainer"] ul{
  padding-left:1.3rem!important;margin:.15rem 0 .55rem!important;
}
[data-testid="stDialog"] [class*="st-key-concierge_"] .chat-author{
  font-weight:850;font-size:10px;letter-spacing:1.25px;color:#667da5;
  text-transform:uppercase;margin:0 0 7px;
}
[data-testid="stDialog"] [class*="st-key-concierge_user_"] .chat-author{color:#087f77}
[data-testid="stDialog"] [role="dialog"]{max-height:92vh!important;overflow-y:auto!important}
/* V17 readability pass: never inherit pale dark-theme text onto white UI. */
.stApp [data-testid="stWidgetLabel"] p,
.stApp [data-testid="stWidgetLabel"] label,
.stApp [data-testid="stRadio"] [data-testid="stMarkdownContainer"] p,
.stApp [data-testid="stCheckbox"] [data-testid="stMarkdownContainer"] p,
.stApp [data-testid="stToggle"] [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] [data-testid="stWidgetLabel"] p,
[data-testid="stDialog"] [data-testid="stRadio"] [data-testid="stMarkdownContainer"] p,
[data-testid="stDialog"] [data-testid="stCheckbox"] [data-testid="stMarkdownContainer"] p {
  color:#192f50!important; -webkit-text-fill-color:#192f50!important;
  opacity:1!important; font-size:14px!important; font-weight:650!important;
}
.stApp [data-testid="stCaptionContainer"] p,
[data-testid="stDialog"] [data-testid="stCaptionContainer"] p {
  color:#4d607e!important;-webkit-text-fill-color:#4d607e!important;
  opacity:1!important;font-size:12.5px!important;line-height:1.5!important;
}
.stApp [data-testid="stMarkdownContainer"] > p,
.stApp [data-testid="stMarkdownContainer"] li,
[data-testid="stDialog"] [data-testid="stMarkdownContainer"] > p,
[data-testid="stDialog"] [data-testid="stMarkdownContainer"] li {
  color:#1b3153!important;-webkit-text-fill-color:#1b3153!important;
  opacity:1!important;line-height:1.6!important;
}
/* Force high-contrast lettering on filled primary buttons (e.g. Check conflicts). */
.stApp button[data-testid="stBaseButton-primary"],
.stApp [data-testid="stBaseButton-primary"],
[data-testid="stDialog"] button[data-testid="stBaseButton-primary"],
[data-testid="stDialog"] [data-testid="stBaseButton-primary"],
.stApp button[kind="primary"],
[data-testid="stDialog"] button[kind="primary"]{
  background:#365bd0!important;color:#fff!important;
  -webkit-text-fill-color:#fff!important;border-color:#365bd0!important;
  font-weight:750!important;opacity:1!important;
}
.stApp [data-testid="stBaseButton-primary"] span,
.stApp [data-testid="stBaseButton-primary"] p,
[data-testid="stDialog"] [data-testid="stBaseButton-primary"] span,
[data-testid="stDialog"] [data-testid="stBaseButton-primary"] p{
  color:#fff!important;-webkit-text-fill-color:#fff!important;
}
.stApp [data-testid="stBaseButton-primary"]:disabled,
[data-testid="stDialog"] [data-testid="stBaseButton-primary"]:disabled{
  background:#b4c1dc!important;color:#3f506a!important;
  -webkit-text-fill-color:#3f506a!important;
}
/* Alerts were dark green/brown with dark-blue text on some machines. */
.stApp [data-testid="stAlert"],
[data-testid="stDialog"] [data-testid="stAlert"]{
  background:#eaf3ff!important; border:1px solid #b9cff0!important;
  border-left:4px solid #3458cf!important; border-radius:12px!important;
  color:#183253!important; opacity:1!important;
}
.stApp [data-testid="stAlert"] p,
.stApp [data-testid="stAlert"] span,
.stApp [data-testid="stAlert"] li,
[data-testid="stDialog"] [data-testid="stAlert"] p,
[data-testid="stDialog"] [data-testid="stAlert"] span,
[data-testid="stDialog"] [data-testid="stAlert"] li{
  color:#183253!important;-webkit-text-fill-color:#183253!important;
  opacity:1!important;font-size:14px!important;font-weight:550!important;
}
.stApp [data-testid="stAlert"] svg,
[data-testid="stDialog"] [data-testid="stAlert"] svg{
  color:#2d55c3!important;stroke:#2d55c3!important;opacity:1!important;
}
/* Help icons next to widget titles were nearly invisible. */
.stApp [data-testid="stTooltipIcon"],
.stApp [data-testid="stTooltipIcon"] svg,
[data-testid="stDialog"] [data-testid="stTooltipIcon"],
[data-testid="stDialog"] [data-testid="stTooltipIcon"] svg,
.stApp button[aria-label*="help" i] svg,
[data-testid="stDialog"] button[aria-label*="help" i] svg{
  color:#31517c!important;stroke:#31517c!important;opacity:1!important;
}
/* Native Streamlit spinners / loading circles: visibly dark blue. */
.stApp [data-testid="stSpinner"],
[data-testid="stDialog"] [data-testid="stSpinner"]{
  color:#173b7b!important;opacity:1!important;
}
.stApp [data-testid="stSpinner"] svg,
[data-testid="stDialog"] [data-testid="stSpinner"] svg,
.stApp [data-testid="stSpinner"] [data-testid="stSpinnerIcon"],
[data-testid="stDialog"] [data-testid="stSpinner"] [data-testid="stSpinnerIcon"]{
  color:#214cb5!important;stroke:#214cb5!important;opacity:1!important;
}
.stApp [data-testid="stSpinner"] p,
[data-testid="stDialog"] [data-testid="stSpinner"] p{
  color:#173b7b!important;-webkit-text-fill-color:#173b7b!important;
  font-weight:700!important;opacity:1!important;
}
/* Select boxes and number inputs should never become black on a light page. */
.stApp [data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stDialog"] [data-testid="stSelectbox"] [data-baseweb="select"] > div,
.stApp [data-testid="stNumberInput"] input,
[data-testid="stDialog"] [data-testid="stNumberInput"] input{
  background:#fff!important;color:#142b4c!important;
  -webkit-text-fill-color:#142b4c!important;border-color:#aebfdb!important;
}
.stApp [data-testid="stSelectbox"] [data-baseweb="select"] span,
[data-testid="stDialog"] [data-testid="stSelectbox"] [data-baseweb="select"] span{
  color:#142b4c!important;-webkit-text-fill-color:#142b4c!important;
}
.stApp [data-testid="stRadio"] [role="radiogroup"],
[data-testid="stDialog"] [data-testid="stRadio"] [role="radiogroup"]{
  gap:13px!important;flex-wrap:wrap!important;
}
.stApp [data-testid="stRadio"] label,
[data-testid="stDialog"] [data-testid="stRadio"] label{opacity:1!important;}
/* An easily readable dataframe is particularly important for slot alternatives. */
.stApp [data-testid="stDataFrame"],
[data-testid="stDialog"] [data-testid="stDataFrame"]{
  background:#fff!important;border:1px solid #c7d5e9!important;
  border-radius:10px!important;
}
@media(max-width:700px){
  [data-testid="stDialog"] [class*="st-key-concierge_"]{width:98%!important;max-width:98%!important}
}

</style>
"""
st.markdown(STYLES, unsafe_allow_html=True)


def go(page: str) -> None:
    st.session_state.studio_page = page
    st.session_state['scroll_to_top_once'] = True
    st.rerun()


def top_navigation() -> None:
    logo, nav_home, nav_studio, nav_results = st.columns(
        [5.5, 1.05, 1.5, 1.35], vertical_alignment="center"
    )
    with logo:
        st.markdown(
            '<div class="app-wordmark"><span class="app-symbol">T</span>'
            '<div><b>Timora Workspace</b><small>SMART ACADEMIC PLANNING</small></div></div>',
            unsafe_allow_html=True,
        )
    with nav_home:
        if st.button("Home", key="go_home", use_container_width=True,
                     type="primary" if st.session_state.studio_page == "home" else "secondary"):
            go("home")
    with nav_studio:
        if st.button("Workspace", key="go_studio", use_container_width=True,
                     type="primary" if st.session_state.studio_page in ("studio", "studio_quality") else "secondary"):
            go("studio")
    with nav_results:
        if st.button("Results", key="go_results", use_container_width=True,
                     disabled=st.session_state.solver_result is None,
                     type="primary" if st.session_state.studio_page == "results" else "secondary"):
            go("results")


@st.cache_data(show_spinner=False)
def workbook_overview(blob: bytes) -> dict:
    """Read only header/record counts; never modifies the workbook."""
    from northcap_cp_sat_solver import excel_records
    data = excel_records(io.BytesIO(blob))
    required = ["Faculty", "Courses", "Student_Groups", "Course_Assignments",
                "Rooms", "Time_Slots", "Faculty_Availability", "Institution_Policies"]
    synthetic = sum(str(r.get("room_id", "")).startswith("SYNTHETIC_LAB") for r in data.get("Rooms", []))
    return {"counts": {name: len(records) for name, records in data.items()},
            "missing": [name for name in required if name not in data],
            "synthetic_labs": synthetic}


def home_page() -> None:
    st.markdown(dedent("""
    <section class="home-hero">
      <div class="home-copy">
        <span class="home-eyebrow">ACADEMIC SCHEDULING · REIMAGINED</span>
        <h1>Make room for<br><em>better schedules.</em></h1>
        <p>A streamlined planning studio that transforms your university dataset
           into organised weekly timetables using constraint programming.</p>
        <div class="hero-mini"><span>Excel-powered</span><span>Conflict checks</span>
          <span>Classrooms &amp; labs</span></div>
      </div>
      <div class="home-art">
        <div class="calendar-demo" aria-label="Illustrative weekly timetable graphic">
          <div class="demo-top"><b>Weekly schedule</b><small>ILLUSTRATION</small></div>
          <div class="demo-week">
            <div><div class="demo-day">MON</div><div class="demo-cell"><div class="demo-pill"></div><div class="demo-pill green"></div><div class="demo-pill"></div></div></div>
            <div><div class="demo-day">TUE</div><div class="demo-cell"><div class="demo-pill green"></div><div class="demo-pill gold"></div><div class="demo-pill"></div></div></div>
            <div><div class="demo-day">WED</div><div class="demo-cell"><div class="demo-pill"></div><div class="demo-pill purple"></div><div class="demo-pill green"></div></div></div>
            <div><div class="demo-day">THU</div><div class="demo-cell"><div class="demo-pill gold"></div><div class="demo-pill"></div><div class="demo-pill purple"></div></div></div>
            <div><div class="demo-day">FRI</div><div class="demo-cell"><div class="demo-pill purple"></div><div class="demo-pill green"></div><div class="demo-pill"></div></div></div>
          </div>
          <div class="demo-cap">One place to plan, explore and export</div>
        </div>
      </div>
    </section>
    """), unsafe_allow_html=True)
    button_col, note_col = st.columns([1.35, 3.25], vertical_alignment="center")
    with button_col:
        if st.button("Open Timora Workspace  →", type="primary", use_container_width=True, key="home_open"):
            go("studio")
    with note_col:
        st.markdown('<p class="hero-cta-note">Start with your existing Excel workbook. No new dataset format is needed.</p>', unsafe_allow_html=True)
    from timetable_persistence import saved_datasets
    if any(x['version_count'] for x in saved_datasets()):
        st.markdown('### Your permanently saved timetables')
        st.caption('Your validated Standard, Fairness, and coordinator-approved timetable versions '
                   'are stored on this computer. Open them without uploading Excel again.')
        if st.button('Open saved timetable library', key='home_open_saved_library',
                     use_container_width=False):
            go('studio')

    st.markdown('<div style="margin-top:38px"><span class="section-eyebrow">BUILT FOR REAL SCHEDULING COMPLEXITY</span>'
                '<h2 class="section-title-large">From scattered inputs to one clear plan.</h2></div>', unsafe_allow_html=True)
    st.markdown(dedent("""
    <div class="value-grid">
      <div class="value-card"><div class="value-icon">01</div><h3>One organised dataset</h3>
      <p>Bring together course hours, faculty, student groups, classrooms, laboratories and available periods in one workbook.</p></div>
      <div class="value-card"><div class="value-icon">02</div><h3>Constraint-aware planning</h3>
      <p>Use Google OR-Tools CP-SAT to schedule teaching sessions under the hard rules in your model and optimise selected preferences.</p></div>
      <div class="value-card"><div class="value-icon">03</div><h3>Schedules worth sharing</h3>
      <p>Explore section-wise timetables and ask the coordinator assistant questions beside your results.</p></div>
    </div>
    <span class="section-eyebrow">YOUR WORKFLOW</span>
    <h2 class="section-title-large">Three steps to your timetable.</h2>
    <div class="how-grid">
      <div class="how-card"><span>STEP 01</span><b>Upload your Excel</b><small>Select the nine-sheet timetable workbook with your scheduling data.</small></div>
      <div class="how-card"><span>STEP 02</span><b>Run the CP-SAT solver</b><small>Choose your settings and generate the weekly teaching-session assignments.</small></div>
      <div class="how-card"><span>STEP 03</span><b>Review and export</b><small>Browse sections, see course–faculty lists and download the results.</small></div>
    </div>
    """), unsafe_allow_html=True)
    st.markdown('<div class="notice-card">Designed for academic timetable prototypes. Source assumptions and simulated resources remain documented in the validation output; always review these before institutional use.</div>', unsafe_allow_html=True)


def run_solver(seconds: int, include_mentor: bool, extra_labs: int,
               optimize_fairness: bool = False, compare_standard: bool = False,
               fairness_priority: str = 'policy_first',
               neighborhood_fraction: float = 0.20,
               fairness_profile: str = 'balanced',
               adaptive_neighborhoods: bool = False) -> None:
    """Generate one verified schedule; optionally also solve a standard baseline.

    Each run uses the same workbook and hard constraints. A baseline comparison
    starts from a separately validated standard CP-SAT timetable. Comparison
    is optional, but the baseline is always retained as a safe fallback.
    """
    blob = st.session_state.workbook_bytes
    if not blob:
        st.warning("Upload your dataset before generating a timetable.")
        return

    def run_once(root, excel_path, variant, fair, caps=None, seed_csv=None,
                 *, alns_operator=None, alns_round=0, limit_seconds=None,
                 original_standard=None, max_change_count=None,
                 change_costs=None, stability_phase=False,
                 fairness_first_phase=False, search_fraction=None):
        output = root / (f'output_alns_{alns_round}' if alns_operator
                         else ('output_fairness' if fair else 'output_standard'))
        run_seconds = int(limit_seconds if limit_seconds is not None else seconds)
        from timetable_identity import safe_workbook_label
        command = [sys.executable, str(Path(__file__).with_name("northcap_cp_sat_solver.py")),
                   "--excel", str(excel_path), "--output", str(output), "--seconds", str(run_seconds),
                   "--dataset-name", safe_workbook_label(st.session_state.workbook_name),
                   "--university-name", st.session_state.get('university_name', '')]
        if fair:
            command.extend(["--optimize-fairness", "--fairness-priority", fairness_priority,
                            "--fairness-focus", fairness_profile])
            if caps is not None:
                command.extend(["--max-avoided-periods", str(caps['avoid']),
                                "--max-late-periods", str(caps['late'])])
            if seed_csv is not None:
                command.extend(['--seed-timetable', str(seed_csv),
                                '--neighborhood-fraction', str(
                                    neighborhood_fraction if search_fraction is None else search_fraction)])
            if alns_operator is not None:
                # ALNS uses fairness-specific objective; hard baseline soft-policy
                # caps keep avoided and late periods from regressing. Manual
                # Fairness retains its original coordinator-selected priority.
                if caps is None or not {'avoid', 'late'} <= set(caps):
                    raise ValueError('Guarded UCB1 requires measured Standard soft-policy caps')
                priority_index = command.index('--fairness-priority') + 1
                command[priority_index] = 'guarded_fairness'
                command.extend(['--alns-operator', alns_operator,
                                '--alns-attempt', str(alns_round),
                                '--room-seconds', str(max(3, min(10, run_seconds // 3)))])
                if original_standard is not None:
                    command.extend(['--standard-reference', str(original_standard)])
                    if max_change_count is not None:  # Legacy experiments only.
                        command.extend(['--max-distinct-changes', str(max_change_count)])
                if change_costs is not None:
                    command.extend(['--change-cost-room', str(change_costs['room']),
                                    '--change-cost-theory', str(change_costs['theory']),
                                    '--change-cost-practical', str(change_costs['practical'])])
                if stability_phase and fairness_first_phase:
                    raise ValueError('Exclusive adaptive Fairness phases requested')
                if stability_phase:
                    command.append('--minimum-disruption-phase')
                elif fairness_first_phase:
                    command.append('--fairness-first-phase')
        if not include_mentor:
            command.append("--exclude-mentoring")
        if extra_labs:
            command += ["--prototype-extra-labs", str(extra_labs)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=(run_seconds + 20 if alns_operator else seconds + 150))
            files = {p.name: p.read_bytes() for p in output.glob("*") if p.is_file()}
            checked = "NOT_RUN"
            if process.returncode == 0 and "timetable_all.csv" in files:
                try:
                    from timetable_validator import validate_bytes, report_as_text
                    verification = validate_bytes(blob, files["timetable_all.csv"], include_mentor, extra_labs)
                    files["independent_validation.json"] = json.dumps(verification, indent=2).encode("utf-8")
                    files["independent_validation.txt"] = report_as_text(verification).encode("utf-8")
                    checked = verification["status"]
                except Exception as exc:
                    checked = "CHECK_ERROR"
                    files["independent_validation.txt"] = (
                        "Independent validation did not complete: " + str(exc)).encode("utf-8")
            return {"status_code": process.returncode, "independent_status": checked,
                    "logs": (process.stdout or "") + "\n" + (process.stderr or ""),
                    "files": files, "variant": variant}
        except subprocess.TimeoutExpired:
            return {"status_code": 99, "independent_status": "NOT_RUN", "files": {},
                    "logs": f"{variant}: solver exceeded the execution time. Increase the time limit.",
                    "variant": variant}
        except Exception as exc:
            return {"status_code": 98, "independent_status": "NOT_RUN", "files": {},
                    "logs": f"{variant}: could not start solver: {type(exc).__name__}: {exc}",
                    "variant": variant}

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        excel_path = root / "input.xlsx"
        excel_path.write_bytes(blob)
        valid_states = {"PASS_WITH_LIMITATIONS", "PASS"}
        baseline = None
        caps = None
        # Fairness starts ONLY after a separate, independently validated
        # Standard generation requested by the coordinator. Reuse its output;
        # never trigger a second Standard solve behind the Fairness button.
        if optimize_fairness:
            previous = st.session_state.get('standard_result')
            same_data = (st.session_state.get('standard_workbook_digest')
                         == st.session_state.get('workbook_digest'))
            same_settings = (previous is not None
                             and previous.get('include_mentor') == include_mentor
                             and previous.get('sim_count') == extra_labs)
            if not (same_data and same_settings and previous.get('status_code') == 0
                    and previous.get('independent_status') in valid_states
                    and 'timetable_all.csv' in previous.get('files', {})):
                st.error('First generate and validate a Standard timetable using this workbook '
                         'and the same mentoring/simulation settings. Then choose Fairness in Workspace.')
                return
            baseline = dict(previous)
            baseline['files'] = dict(previous['files'])
            standard_dir = root / 'output_standard'
            standard_dir.mkdir(exist_ok=True)
            (standard_dir / 'timetable_all.csv').write_bytes(
                baseline['files']['timetable_all.csv'])
            if 'quality_summary.json' in baseline['files']:
                from timetable_fairness import quality_caps_from_report
                standard_quality = json.loads(
                    baseline['files']['quality_summary.json'].decode('utf-8'))
                caps = quality_caps_from_report(standard_quality)

        seed_csv = (root / "output_standard" / "timetable_all.csv"
                    if baseline is not None
                    and baseline["status_code"] == 0
                    and baseline["independent_status"] in valid_states
                    and "timetable_all.csv" in baseline["files"] else None)
        alns_summary = None
        if optimize_fairness and adaptive_neighborhoods and baseline is not None:
            from timetable_bandit_alns import learning_context, run_adaptive_search, validate_change_costs
            from timetable_records import data_digest
            # No arbitrary timetable-change cap and no preset fairness target.
            # Preserve the original Standard as a soft stability reference.
            # Fixed bounded prototype weights; editable in the code if an
            # institution confirms different change costs.
            change_costs = validate_change_costs()
            context_key = learning_context(seconds=seconds, fraction=neighborhood_fraction,
                profile=fairness_profile, priority='guarded_fairness',
                include_mentor=include_mentor, extra_labs=extra_labs,
                change_costs=change_costs)
            # Section-gap operators are offered only if the Excel enables that
            # soft preference. All other neighborhoods retain the original
            # solver's hard constraints and validated time/room candidates.
            from timetable_fairness import available_focuses_from_policy_report
            try:
                policy_data = json.loads(baseline['files']['policy_compliance.json'])
            except (KeyError, ValueError, TypeError):
                policy_data = {}
            operators = ['faculty_gap_repair', 'workload_balance_repair', 'late_period_repair']
            if 'section_gaps' in available_focuses_from_policy_report(policy_data):
                operators.insert(1, 'section_gap_repair')

            def solve_neighborhood(operator, incumbent, round_index, attempt_seconds):
                seed = root / f'alns_incumbent_{round_index}.csv'
                seed.write_bytes(incumbent['files']['timetable_all.csv'])
                # Gradually open larger local neighborhoods only when needed.
                # These are SEARCH windows, not limits on final class changes.
                window = min(neighborhood_fraction, (0.12, 0.18, 0.26, 0.35)[min(round_index-1, 3)])
                found = run_once(root, excel_path, 'fairness', True, caps=caps,
                                seed_csv=seed, alns_operator=operator,
                                alns_round=round_index, limit_seconds=attempt_seconds,
                                original_standard=seed_csv, max_change_count=None,
                                change_costs=change_costs, search_fraction=window)
                found['neighborhood_fraction'] = window
                return found

            with st.spinner('UCB1 is guiding short, independently validated CP-SAT improvement searches...'):
                primary, alns_summary = run_adaptive_search(
                    baseline=baseline, solve=solve_neighborhood,
                    dataset_hash=data_digest(blob), context_key=context_key,
                    profile=fairness_profile, caps=caps, seconds=seconds,
                    operators=operators, max_rounds=4,
                    change_costs=change_costs)
        else:
            activity = ('Optimizing Fairness from the validated Standard timetable...'
                        if optimize_fairness else 'Generating and validating the Standard CP-SAT timetable...')
            with st.spinner(activity):
                primary = run_once(root, excel_path,
                                   'fairness' if optimize_fairness else 'standard',
                                   optimize_fairness, caps=caps, seed_csv=seed_csv)
        if baseline is not None and seed_csv is not None:
            from timetable_recovery import select_published_result
            primary = select_published_result(baseline, primary, caps, fairness_profile=fairness_profile)
        if alns_summary is not None:
            primary['files'] = dict(primary['files'])
            primary['files']['alns_learning_report.json'] = json.dumps(
                alns_summary, indent=2).encode('utf-8')
            from timetable_bandit_alns import final_change_csv
            primary['files']['alns_final_changes.csv'] = final_change_csv(
                alns_summary['final_changed_sessions'])
            stream = io.StringIO()
            rounds = alns_summary['rounds']
            if rounds:
                writer = csv.DictWriter(stream, fieldnames=list(rounds[0]))
                writer.writeheader()
                writer.writerows(rounds)
            primary['files']['alns_learning_rounds.csv'] = stream.getvalue().encode('utf-8-sig')
            primary['alns_summary'] = alns_summary
            primary['logs'] = ('UCB1-ALNS attempted ' + str(alns_summary['attempted'])
                + ' neighborhoods; ' + str(alns_summary['improved'])
                + ' validated improvements accepted.\n' + primary.get('logs', ''))
        primary["seconds"] = seconds
        primary["neighborhood_fraction"] = neighborhood_fraction if optimize_fairness else None
        primary["sim_count"] = extra_labs
        primary["include_mentor"] = include_mentor
        primary["input_file"] = st.session_state.workbook_name
        primary["university_name"] = st.session_state.get("university_name", "")
        primary["fairness_enabled"] = optimize_fairness and not primary.get("fell_back_to_standard", False)
        primary["fairness_requested"] = optimize_fairness
        primary["fairness_priority"] = (
            'guarded_fairness' if optimize_fairness and adaptive_neighborhoods
            else fairness_priority if optimize_fairness else 'standard')
        primary["fairness_profile"] = fairness_profile if optimize_fairness else "balanced"
        # Optional focus advisors have been removed. The coordinator or UCB1
        # neighborhood search determines the fairness settings.
        primary['fairness_alns_enabled'] = bool(optimize_fairness and adaptive_neighborhoods)
        if optimize_fairness:
            from timetable_fairness import weights_for_profile
            primary["files"]["fairness_focus_summary.json"] = json.dumps({
                "focus": fairness_profile,
                "selected_by": "Coordinator/manual fairness setting",
                "weights": weights_for_profile(fairness_profile),
                "baseline_guardrails": caps,
                'published_fairness_candidate': not primary.get('fell_back_to_standard', False),
                'adaptive_neighborhoods': bool(adaptive_neighborhoods),
            }, indent=2).encode("utf-8")
        primary["comparison_requested"] = compare_standard
        primary["comparison_guards"] = caps
        if baseline is not None:
            primary["baseline_status_code"] = baseline["status_code"]
            primary["baseline_log"] = baseline["logs"]
            primary["baseline_independent_status"] = baseline["independent_status"]
            for name, data in baseline["files"].items():
                primary["files"]["baseline_" + name] = data
            if (compare_standard
                    and baseline["status_code"] == 0
                    and baseline["independent_status"] in valid_states
                    and primary["status_code"] == 0
                    and primary["independent_status"] in valid_states
                    and not primary.get("fell_back_to_standard", False)
                    and "quality_summary.json" in primary["files"]
                    and "quality_summary.json" in baseline["files"]):
                from timetable_fairness import quality_comparison
                fairness = json.loads(primary["files"]["quality_summary.json"].decode("utf-8"))
                standard = json.loads(baseline["files"]["quality_summary.json"].decode("utf-8"))
                comparison = quality_comparison(standard, fairness)
                primary["files"]["quality_comparison.json"] = json.dumps(
                    comparison, indent=2).encode("utf-8")
                output_text = "Standard vs fairness: negative change means fewer occurrences.\n\n"
                output_text += "\n".join(
                    f"{r['metric']}: standard {r['standard']}, fairness {r['fairness']}, change {r['change']:+}"
                    for r in comparison)
                primary["files"]["quality_comparison.txt"] = output_text.encode("utf-8")
                primary["comparison"] = comparison
                if caps is not None:
                    # Verify actual output metrics, not merely CP-SAT status.
                    qa = fairness['metrics']
                    if (qa['preferred_free_slot_periods'] > caps['avoid']
                            or qa['late_session_periods'] > caps['late']):
                        primary['status_code'] = 6
                        primary['comparison_unavailable'] = (
                            'Fairness output exceeded a validated baseline cap; '
                            'the result has been rejected for review.')
                        primary.pop('comparison', None)
                        primary['files'].pop('quality_comparison.json', None)
                        primary['files'].pop('quality_comparison.txt', None)
            elif compare_standard:
                primary.setdefault("comparison_unavailable",
                    "The baseline or optimized timetable did not pass independent validation. "
                    "No comparison is reported as proven.")
        elif compare_standard and optimize_fairness:
            primary["comparison_unavailable"] = (
                "The standard baseline could not be independently validated. "
                "Optimization proceeded without baseline-based late/avoid guardrails."
            )
    # Persist the uploaded workbook and every independently validated Standard/
    # Fairness result. Coordinator edits retain their existing atomic audit saves.
    if (primary.get('status_code') == 0 and
            primary.get('independent_status') in ('PASS', 'PASS_WITH_LIMITATIONS') and
            st.session_state.get('workbook_bytes')
            and __import__('zipfile').is_zipfile(
                __import__('io').BytesIO(st.session_state.workbook_bytes))):
        try:
            from timetable_persistence import save_workbook, archive_validated_result
            save_workbook(st.session_state.workbook_bytes,
                          st.session_state.workbook_name,
                          st.session_state.university_name)
            from timetable_persistence import read_workbook, ensure_version_files
            versions = archive_validated_result(st.session_state.workbook_bytes, primary)
            from timetable_records import data_digest as _persistent_digest
            stored = read_workbook(_persistent_digest(st.session_state.workbook_bytes))
            for saved_id in versions:
                try:
                    ensure_version_files(stored, saved_id)
                except (ImportError, ValueError, RuntimeError, OSError) as export_error:
                    st.warning('Schedule archived, but printable files could not be prepared: '
                               + str(export_error))
        except Exception as exc:
            st.warning('Timetable generated, but permanent save failed: ' + str(exc))
    st.session_state.solver_result = primary
    if (not optimize_fairness and primary['status_code'] == 0
            and primary['independent_status'] in ('PASS', 'PASS_WITH_LIMITATIONS')
            and 'timetable_all.csv' in primary['files']):
        st.session_state.standard_result = primary
        st.session_state.standard_workbook_digest = st.session_state.workbook_digest
    elif not optimize_fairness:
        # A failed Standard rerun must not leave a stale Fairness launch ready.
        st.session_state.standard_result = None
        st.session_state.standard_workbook_digest = None
    st.session_state['reset_timetable_variant_on_next_run'] = True
    st.session_state.pop('last_manual_save_message', None)
    st.session_state.pop('concierge_previous_source', None)
    st.session_state.coordinator_preview = None
    st.session_state.coordinator_last_diagnosis = ""
    st.session_state.assistant_history = []
    st.session_state.studio_page = "results"
    # Avoid preserving a scroll position from the end of the Workspace form.
    st.session_state["scroll_to_top_once"] = True
    if "preview_section" in st.session_state:
        del st.session_state["preview_section"]
    st.rerun()


def verified_standard_ready() -> bool:
    """A Fairness button can run only from the current, validated Standard baseline.

    Results are on a different page from Workspace; read the original solver
    settings stored with the Standard result rather than new widget defaults.
    """
    standard = st.session_state.get('standard_result')
    return bool(
        standard is not None
        and st.session_state.get('workbook_bytes') is not None
        and st.session_state.get('standard_workbook_digest') is not None
        and st.session_state.get('standard_workbook_digest')
            == st.session_state.get('workbook_digest')
        and standard.get('status_code') == 0
        and standard.get('independent_status') in ('PASS', 'PASS_WITH_LIMITATIONS')
        and 'timetable_all.csv' in standard.get('files', {})
        and 'timetable_sections.html' in standard.get('files', {})
    )



def restore_standard_baseline_from_archive() -> bool:
    """Reopen a saved Standard schedule for Fairness, without solving again.

    The archived schedule is independently revalidated by restore_version.
    The currently displayed result (including Fairness or coordinator changes)
    is never replaced. Only the Fairness baseline is restored.
    """
    from timetable_records import list_versions
    from timetable_persistence import read_workbook, restore_version

    digest = st.session_state.get('workbook_digest')
    workbook_bytes = st.session_state.get('workbook_bytes')
    if not digest or not workbook_bytes:
        return False
    standards = [v for v in list_versions(digest) if v['variant'] == 'STANDARD']
    if not standards:
        return False
    stored = read_workbook(digest)
    if stored is None or stored['workbook_blob'] != workbook_bytes:
        return False
    restored = restore_version(stored, standards[0]['version_id'])
    if (restored.get('status_code') != 0
            or restored.get('independent_status') not in ('PASS', 'PASS_WITH_LIMITATIONS')
            or 'timetable_all.csv' not in restored.get('files', {})):
        raise ValueError('Saved Standard timetable was not independently validated')
    st.session_state.standard_result = restored
    st.session_state.standard_workbook_digest = digest
    return True


def workspace_page() -> None:
    st.markdown(dedent("""
    <div class="workspace-banner"><span class="section-eyebrow">THE SCHEDULING WORKSPACE</span>
      <h1>Build your university timetable.</h1>
      <p>Upload the workbook, adjust your preferences and let CP-SAT assemble the weekly schedule.</p></div>
    <div class="steps-strip">
      <div class="steps-item"><span class="steps-num">01</span><div><b>Import workbook</b><small>Read course and resource mappings</small></div></div>
      <div class="steps-item"><span class="steps-num">02</span><div><b>Generate schedule</b><small>Apply your scheduling model</small></div></div>
      <div class="steps-item"><span class="steps-num">03</span><div><b>Explore results</b><small>Review sections and exports</small></div></div>
    </div>
    <span class="section-eyebrow">YOUR DATASET</span>
    <h2 class="section-title-large">Upload your scheduling workbook</h2>
    <p class="section-subtitle">Choose your university timetable dataset in Excel format. Your workbook is read directly; this interface does not rewrite it.</p>
    """), unsafe_allow_html=True)
    from timetable_persistence import saved_datasets, read_workbook, restore_version
    stored_workbooks = [entry for entry in saved_datasets() if entry['version_count']]
    if stored_workbooks:
        with st.expander('Open permanently saved timetable', expanded=st.session_state.workbook_bytes is None):
            available_datasets = {r['dataset_hash']: r for r in stored_workbooks}
            saved_digest = st.selectbox('Saved university workbook', list(available_datasets),
                format_func=lambda d: f"{available_datasets[d]['filename']} · {available_datasets[d]['version_count']} saved version(s)",
                key='permanent_saved_dataset')
            from timetable_records import list_versions
            saved_entries = list_versions(saved_digest)
            saved_vid = st.selectbox('Saved timetable version', [v['version_id'] for v in saved_entries],
                format_func=lambda vid: next(f"{v['variant']} · {v['created_at']}" for v in saved_entries if v['version_id'] == vid),
                key='permanent_saved_version')
            if st.button('Open saved timetable', key='open_permanent_saved', type='primary'):
                try:
                    stored = read_workbook(saved_digest)
                    with st.spinner('Verifying saved timetable and rebuilding reports...'):
                        restored = restore_version(stored, saved_vid)
                    st.session_state.workbook_bytes = stored['workbook_blob']
                    st.session_state.workbook_name = stored['filename']
                    st.session_state.workbook_digest = saved_digest
                    st.session_state.university_name = stored['university_name']
                    st.session_state['university_name_input'] = stored['university_name']
                    st.session_state.solver_result = restored
                    st.session_state.standard_result = restored if not restored['fairness_enabled'] and not restored['rescheduled'] else None
                    st.session_state.standard_workbook_digest = saved_digest if st.session_state.standard_result else None
                    # Restoring a Fairness or coordinator timetable should not
                    # hide its separately archived Standard baseline from UCB1.
                    if st.session_state.standard_result is None:
                        try:
                            restore_standard_baseline_from_archive()
                        except (OSError, ValueError, KeyError, TypeError, __import__('sqlite3').DatabaseError) as baseline_error:
                            st.warning('Saved timetable opened, but the Standard baseline could not be restored for Fairness: '
                                       + str(baseline_error))
                    st.session_state.studio_page = 'results'
                    st.session_state['reset_timetable_variant_on_next_run'] = True
                    st.session_state['scroll_to_top_once'] = True
                    st.rerun()
                except Exception as exc:
                    st.error('Could not reopen saved timetable: ' + str(exc))
    uploaded = st.file_uploader("Upload your university timetable dataset", type=["xlsx"],
                                key=f"workbook_upload_{st.session_state.upload_version}",
                                help="Expected sheets: Faculty, Courses, Student_Groups, Course_Assignments, Rooms, Time_Slots, Faculty_Availability, Institution_Policies and Data_Sources.")
    if uploaded is not None:
        blob = uploaded.getvalue()
        digest = hashlib.sha256(blob).hexdigest()
        if (st.session_state.workbook_digest != digest
                or st.session_state.workbook_name != uploaded.name):
            st.session_state.workbook_bytes = blob
            st.session_state.workbook_name = uploaded.name
            from timetable_identity import suggested_university_name
            st.session_state.university_name = suggested_university_name(uploaded.name)
            st.session_state['university_name_input'] = st.session_state.university_name
            st.session_state.workbook_digest = digest
            from timetable_persistence import save_workbook
            save_workbook(blob, uploaded.name, st.session_state.university_name)
            st.session_state.solver_result = None
            st.session_state.standard_result = None
            st.session_state.standard_workbook_digest = None
            st.session_state.pop('last_manual_save_message', None)
            st.session_state.coordinator_preview = None
            st.session_state.coordinator_last_diagnosis = ""
            st.session_state.assistant_history = []
    if st.session_state.workbook_bytes is not None:
        filename = html.escape(st.session_state.workbook_name or "selected.xlsx")
        institution = st.text_input(
            'University / institution name (shown on timetable reports)',
            key='university_name_input',
            help='Optional label for the report. If blank, the report says Not specified. '
                 'This does not change the uploaded Excel or scheduling rules.')
        st.session_state.university_name = institution.strip()
        st.markdown(f'<div class="note-card"><b>Workbook ready:</b> {filename}<br>'
                    'Your file stays selected while you move between Home, Workspace and Results.</div>', unsafe_allow_html=True)
        try:
            overview = workbook_overview(st.session_state.workbook_bytes)
            counts = overview["counts"]
            items = st.columns(4)
            for col, sheet, label in zip(items, ["Faculty", "Courses", "Student_Groups", "Rooms"],
                                         ["Faculty", "Courses", "Student groups", "Rooms"]):
                col.metric(label, counts.get(sheet, 0))
            if overview["missing"]:
                st.error("Missing required Excel sheets: " + ", ".join(overview["missing"]))
            elif overview["synthetic_labs"]:
                st.caption(f"This workbook contains {overview['synthetic_labs']} synthetic laboratory records. "
                           "These remain prototype resources; no extra simulation is needed for them.")
        except Exception as exc:
            st.error(f"Could not read workbook overview: {exc}")
        if st.button("Clear selected workbook", key="clear_workbook"):
            st.session_state.workbook_bytes = None
            st.session_state.workbook_name = None
            st.session_state.university_name = ''
            st.session_state.workbook_digest = None
            st.session_state.solver_result = None
            st.session_state.standard_result = None
            st.session_state.standard_workbook_digest = None
            st.session_state.pop('last_manual_save_message', None)
            st.session_state.coordinator_preview = None
            st.session_state.coordinator_last_diagnosis = ""
            st.session_state.assistant_history = []
            st.session_state.upload_version += 1
            st.rerun()

    try:
        from northcap_cp_sat_solver import excel_records
        from timetable_policies import load_policies
        import io as _io
        policy_config = load_policies(excel_records(_io.BytesIO(st.session_state.workbook_bytes))) if st.session_state.workbook_bytes else None
    except Exception:
        policy_config = None
    if policy_config:
        if policy_config.errors:
            st.error("Some enabled Excel policies are unsupported or inconsistent. Generation will stop until they are corrected. Open the policy audit after running validation.")
        st.caption("Open Elective window: " + policy_config.elective_window_mode
                   + " | Allowed course IDs: " + (", ".join(sorted(policy_config.window_allowed_course_codes)) or "none")
                   + " | Protected slots: " + (", ".join(sorted(policy_config.reserved_slot_ids)) or "none"))
        st.caption("Excel-defined soft-avoid slots: " + (", ".join(sorted(policy_config.avoid_slot_ids)) or "none")
                   + f" | penalty per occupied period: {policy_config.avoid_weight}. "
                   "All weekly timetables use the institution's Time_Slots calendar.")
    st.markdown('<div style="margin-top:26px"><span class="section-eyebrow">SOLVER SETTINGS</span>'
                '<h2 class="section-title-large">Prepare your Standard timetable</h2>'
                '<p class="section-subtitle">Keep every workbook-defined hard scheduling rule. '
                'To optimize Fairness separately, generate Standard first, then use the option below its timetable in Results.</p></div>', unsafe_allow_html=True)
    with st.expander("Solver settings", expanded=True):
        a, b = st.columns(2)
        with a:
            seconds = st.slider("Solver time limit (seconds)", min_value=30, max_value=300, value=120, step=30)
            mentor_frequency = policy_config.mentor_weeks if policy_config else 2
            include_mentor = st.checkbox(
                f"Include mentor sessions (week 1 of {mentor_frequency or 1}-week cycle)", value=True,
                disabled=(mentor_frequency == 1),
                help="Enable mentor sessions in the selected week; other weeks of a multi-week cycle omit them. For weekly mentoring this is always required.")
        with b:
            st.markdown("**Optional simulation**")
            simulate = st.checkbox("Add extra fictional 30-seat labs during this run", value=False,
                                   help="Testing only. This does not change your Excel dataset.")
            extra_labs = st.slider("Additional simulated laboratories", min_value=1, max_value=10, value=1) if simulate else 0
            if simulate:
                st.caption("Simulation-only capacity. Results are not proof of real-room availability.")
    # Standard is the only default generation action. Fairness has its OWN
    # opt-in control and does not start until a verified Standard run exists.
    st.markdown('### Standard CP-SAT generation')
    st.caption('Generate the Standard timetable first. No Fairness solve will run automatically.')
    st.markdown('<div class="notice-card">Scheduling rules come from the uploaded workbook. '
                'Unsupported enabled policies stop generation with a readable report.</div>',
                unsafe_allow_html=True)
    if st.button('Generate Standard Timetable  →', type='primary',
                 disabled=st.session_state.workbook_bytes is None,
                 use_container_width=True, key='run_cp_sat'):
        run_solver(seconds, include_mentor, extra_labs,
                   optimize_fairness=False, compare_standard=False)




def workspace_quality_page() -> None:
    """A separate workspace destination for soft quality settings."""
    st.markdown('<div class="workspace-banner"><span class="section-eyebrow">WORKSPACE / QUALITY SETTINGS</span>'
                '<h1>Timetable Quality</h1>'
                '<p>Choose Fairness settings for an optional, separate run after Standard generation. '
                'The uploaded Excel remains the source of scheduling rules.</p></div>',
                unsafe_allow_html=True)
    st.caption("Choose whether to optimize gaps and late periods. These are soft preferences; "
               "they never override hard constraints, faculty assignments or course hours.")
    prefs = st.session_state.get('quality_saved', {
        'optimize': True, 'compare': False,
        'priority': 'Protect policy and late periods first'})
    for widget, pref in (('quality_optimize', 'optimize'),
                         ('quality_compare', 'compare'),
                         ('quality_priority', 'priority')):
        if widget not in st.session_state:
            st.session_state[widget] = prefs[pref]
    # Fairness is controlled by the separate Generate Fairness button in Workspace.
    # Retain the legacy key for old settings/tests, but do not expose an
    # 'Enable Fairness' checkbox that implicitly changes Standard generation.
    st.session_state['quality_optimize'] = True
    st.session_state['quality_compare'] = True
    st.caption('Standard vs Fairness comparisons are automatic in the separate comparison dashboard.')
    st.selectbox(
        "Fairness trade-off strategy",
        ["Protect policy and late periods first", "Balanced weights from Excel"],
        key="quality_priority",
        help="Protected mode prefers Excel soft-avoid periods and fewer late classes; balanced uses Excel's soft weights.")
    st.info("Fairness reuses the validated Standard timetable from the current workbook. "
            "It never runs automatically or re-generates Standard in the background.")
    st.session_state['quality_saved'] = {
        'optimize': bool(st.session_state.get('quality_optimize', True)),
        'compare': bool(st.session_state.get('quality_compare', False)),
        'priority': st.session_state.get('quality_priority', 'Protect policy and late periods first'),
    }
    st.caption("Changing these settings does not edit an existing timetable. "
               "Open Results → Timetable explorer to start a separate Fairness run below the timetable.")
    if st.button("Back to Workspace / Generate timetable", type="primary", key="quality_return"):
        go("studio")

@st.cache_data(show_spinner=False, max_entries=5)
def _cached_full_pdf_exports(csv_blob: bytes, workbook_blob: bytes, university_name: str,
                             dataset_name: str, timetable_label: str,
                             moved_ids: tuple[str, ...] = ()) -> dict[str, bytes]:
    """PDFs reflect the selected validated CSV, never a regenerated timetable."""
    from timetable_pdf_export import build_pdf_exports
    return build_pdf_exports(csv_blob, workbook_blob, university_name=university_name,
                             dataset_name=dataset_name, version_name=timetable_label,
                             highlighted_session_ids=moved_ids)


def _result_zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for filename, content in sorted(files.items()):
            archive.writestr(filename, content)
    return out.getvalue()


def _maybe_explain_complex_question(question: str, verified: str, files: dict[str, bytes]) -> str:
    """Offer one Groq-composed factual answer, preserving the local fallback."""
    if not (st.session_state.get('agent_use_llm', False)
            and st.session_state.get('groq_analytical_answers', True)):
        return verified
    from timetable_cloud_ai import get_api_key
    if not get_api_key():
        return verified + '\n\n*Verified local response · connect Groq in Cloud AI settings to enable AI answers*'
    # Use Groq for both simple and analytical read-only answers, not only
    # questions containing words such as "explain" or "compare". The full
    # selected timetable is never sent; only the checked tool finding.
    from timetable_response_composer import one_groq_answer
    enhanced, used = one_groq_answer(
        question, verified, all_questions=True,
        model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
    if used:
        return enhanced + '\n\n*Groq response · timetable facts verified by Python*'
    return verified + '\n\n*Verified local response · Groq did not generate this answer*'


def _answer_question(question: str, files: dict[str, bytes], *,
                     source_label: str = '', comparison_files=None) -> str:
    """Verified schedule answer, with optional GroqCloud intent classification."""
    from timetable_agent import answer
    from northcap_cp_sat_solver import excel_records
    if not st.session_state.workbook_bytes:
        return "Please upload a timetable workbook and generate a schedule first."
    try:
        from timetable_source_answers import answer_source_query
        source_answer = answer_source_query(question, source_label=source_label,
                                           comparison_files=comparison_files)
        if source_answer is not None:
            return _maybe_explain_complex_question(question, source_answer, files)
        rows = list(csv.DictReader(io.StringIO(files["timetable_all.csv"].decode("utf-8-sig"))))
        data = excel_records(io.BytesIO(st.session_state.workbook_bytes))
        report_bytes = files.get("independent_validation.json")
        report = json.loads(report_bytes.decode("utf-8")) if report_bytes else None
        from timetable_chat_intents import answer_requested_placement
        placement = answer_requested_placement(
            question, rows, data,
            include_mentoring=st.session_state.get('solver_result', {}).get('include_mentor', True),
            extra_labs=st.session_state.get('solver_result', {}).get('sim_count', 0))
        if placement is not None:
            return placement
        reply = answer(question, rows, data, report,
                       llm_enabled=st.session_state.get('agent_use_llm', False),
                       model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'),
                       recent_diagnosis=st.session_state.get('coordinator_last_diagnosis'),
                       conversation=st.session_state.get('assistant_history', []))
        verified = reply.strip() or "No answer available. Try a more specific timetable question."
        return _maybe_explain_complex_question(question, verified, files)
    except Exception as exc:
        return ("I couldn't access the current timetable data. "
                f"Details: {type(exc).__name__}: {exc}")


def _record_assistant_turns(messages: list[dict]) -> None:
    """Append chat messages and save them to the current timetable's local history."""
    st.session_state.assistant_history.extend(messages)
    source_key = st.session_state.get('assistant_chat_source_key')
    digest = st.session_state.get('assistant_chat_workbook_digest')
    if not source_key or not digest:
        return
    try:
        from timetable_chat_history import create_conversation, append_messages
        ident = st.session_state.get('assistant_chat_thread_id')
        if not ident:
            ident = create_conversation(digest, source_key)
            st.session_state['assistant_chat_thread_id'] = ident
        append_messages(digest, ident, source_key, messages)
    except (OSError, ValueError, sqlite3.Error) as exc:
        st.session_state['assistant_chat_history_warning'] = (
            'This chat is visible for this session, but local history could not save: ' + str(exc))


def _render_chat_bubble(role: str, content: str, index: int) -> None:
    """Native Markdown renderer inside a styled bubble (no escaped bold or italic markers)."""
    author = "You" if role == "user" else "Timora"
    # Use Streamlit's native Markdown renderer rather than injecting escaped
    # source text into HTML. This retains lists, bold, italics, and paragraphs.
    with st.container(key=f"concierge_{role}_{index}", border=False):
        st.markdown(f'<div class="chat-author">{author}</div>', unsafe_allow_html=True)
        # Strip a known stray UI artifact if it appears in model/source text.
        cleaned = re.sub(r'(?im)^\s*canvascanvas\s*$', '', content or '').strip()
        st.markdown(cleaned, unsafe_allow_html=False)


@st.dialog("Timora", width="large")
def show_coordinator_chat() -> None:
    """Premium floating chat for timetable Q&A."""
    result = st.session_state.get("solver_result")
    if not result or "timetable_all.csv" not in result.get("files", {}):
        st.warning("Generate a timetable before opening Timora.")
        return
    from timetable_versions import context_result
    from timetable_records import data_digest, get_version, list_versions
    live_view = context_result(result, st.session_state.get('timetable_variant'))
    files = dict(live_view['files'])
    digest = data_digest(st.session_state.workbook_bytes)
    archive = list_versions(digest)
    archive_by_id = {v['version_id']: v for v in archive}
    source_ids = ['active'] + list(archive_by_id)
    source = st.selectbox(
        'Timetable to analyze', source_ids,
        format_func=lambda v: (f"Current — {live_view['_active_label']}" if v == 'active'
                               else f"Saved — {archive_by_id[v]['variant']} · {v}"),
        key='concierge_source_v116',
        help='The assistant uses only the selected timetable CSV. Saved versions are read-only.')
    if st.session_state.get('concierge_previous_source') != (source, live_view['_active_source_csv_key']):
        st.session_state.concierge_previous_source = (source, live_view['_active_source_csv_key'])
        st.session_state.pop('chat_move_state', None)
        st.session_state.pop('assistant_move_reply', None)
        st.session_state.pop('assistant_move_preview', None)
        st.session_state.pop('assistant_conflict_state', None)
    if source != 'active':
        source_label = f"saved {archive_by_id[source]['variant']} version {source}"
        comparison_files = None
        stored = get_version(digest, source)
        if stored is None:
            st.error('That saved timetable is unavailable. Please choose another version.')
            return
        files['timetable_all.csv'] = stored['csv_blob']
        # The current run's validation report must never be passed off as a
        # validation report for a saved historical timetable.
        files.pop('independent_validation.json', None)
        st.markdown(f'<div class="premium-inline-note"><b>Reading saved timetable:</b> {html.escape(source)}. This historical CSV is read-only here. To request a class change, open Saved runs → Open as working copy.</div>', unsafe_allow_html=True)
    else:
        source_label = f"current {live_view['_active_label']} timetable"
        comparison_files = result.get('files', {}) if result.get('fairness_enabled') and not result.get('fell_back_to_standard') else None
        st.markdown(f'<div class="premium-inline-note"><b>Reading current timetable:</b> {html.escape(live_view["_active_label"])}. Ask Timora to move a class directly in this chat or use <b>Shift a Class</b>. Only a verified option and your explicit approval can update the schedule.</div>', unsafe_allow_html=True)
    # History is stored locally for the workbook and a specific timetable
    # snapshot. A new solver output cannot silently inherit old facts.
    from timetable_chat_history import (list_conversations, load_messages,
                                        delete_conversation)
    from timetable_rescheduling import schedule_hash as _chat_schedule_hash
    source_key = f"{source}:{_chat_schedule_hash(files['timetable_all.csv'])}"
    history_context = (digest, source_key)
    if st.session_state.get('assistant_chat_context') != history_context:
        st.session_state['assistant_chat_context'] = history_context
        st.session_state['assistant_chat_source_key'] = source_key
        st.session_state['assistant_chat_workbook_digest'] = digest
        st.session_state.pop('assistant_chat_thread_id', None)
        st.session_state['assistant_history'] = []
        st.session_state.pop('assistant_chat_force_new', None)
        st.session_state.pop('chat_history_choice', None)
        st.session_state.pop('chat_move_state', None)
    try:
        available_chats = list_conversations(digest)
    except (OSError, sqlite3.Error):
        available_chats = []
    matching_chats = [chat for chat in available_chats if chat['source'] == source_key]
    if not st.session_state.get('assistant_chat_thread_id') and not st.session_state.get('assistant_chat_force_new') and matching_chats:
        st.session_state['assistant_chat_thread_id'] = matching_chats[0]['id']
        st.session_state['assistant_history'] = load_messages(digest, matching_chats[0]['id'])
    elif st.session_state.get('assistant_chat_thread_id') and not st.session_state.get('assistant_history'):
        # Some older navigation paths clear only the in-memory chat list.
        # Recover its matching, locally saved conversation on the next visit.
        st.session_state['assistant_history'] = load_messages(
            digest, st.session_state['assistant_chat_thread_id'])

    # A confirmed update is a new source-grounded assistant event, not a
    # hypothetical response. Show it in chat after the revised timetable loads.
    latest_move = st.session_state.get('assistant_last_published_detail')
    if source == 'active' and latest_move:
        from timetable_rescheduling import schedule_hash as current_schedule_hash
        if latest_move.get('schedule_hash') == current_schedule_hash(files['timetable_all.csv']):
            st.success('An approved class move has been saved. The timetable below is the revised version.')
            if st.session_state.get('assistant_move_notice_shown_for') != latest_move['version_id']:
                _record_assistant_turns([{
                    'role': 'assistant', 'content': latest_move['message'],
                }])
                st.session_state['assistant_move_notice_shown_for'] = latest_move['version_id']
    st.radio('Timora tools',
             ['Chat & Tasks', 'Find Free Slots', 'Shift a Class', 'Check Conflicts'],
             horizontal=True, key='assistant_feature',
             help='Choose an operation. Only confirmed, independently validated class moves change the timetable.')
    with st.expander('Cloud AI settings (Groq)', expanded=False):
        from timetable_cloud_ai import get_api_key, check_connection, ALLOWED_MODELS, DEFAULT_MODEL
        st.toggle('Groq answers from verified timetable data', value=True,
                  key='groq_analytical_answers',
                  help='For regular read-only questions, Groq composes one answer from verified Python timetable facts; fallback to the local answer if unavailable. Class moves remain validated.')
        st.toggle('Use GroqCloud AI understanding', key='agent_use_llm',
                  help='Timora works without cloud AI. Turn this on to use Groq for optional language understanding and explanations.')
        # Keep the connection controls visible even when AI is toggled off. A
        # password widget must not vanish on rerun merely because it now has a value.
        if st.session_state.get('agent_model') not in ALLOWED_MODELS:
            st.session_state['agent_model'] = DEFAULT_MODEL
        st.selectbox('Groq cloud model',
                     [DEFAULT_MODEL, 'openai/gpt-oss-20b'],
                     key='agent_model',
                     help='Cloud-hosted model. No local Ollama installation is needed.')
        with st.form('groq_connection_form', clear_on_submit=False):
            st.text_input('Paste GroqCloud API key', type='password',
                          key='groq_api_key', placeholder='gsk_...',
                          help='Paste a key generated at console.groq.com/keys. Never share it in a chat message.')
            connect_clicked = st.form_submit_button('Connect Groq / Test key', type='primary')
        if connect_clicked:
            # Submit happens *after* Streamlit commits the form value. The
            # explicit key entered above takes priority over configured keys.
            with st.spinner('Checking your GroqCloud API key...'):
                ok, message = check_connection(model=st.session_state.agent_model)
            import hashlib as _hashlib
            st.session_state['groq_connection_status'] = {
                'ok': ok, 'message': message,
                'model': st.session_state.agent_model,
                'key_hash': _hashlib.sha256(get_api_key().encode('utf-8')).hexdigest(),
            }
        connection = st.session_state.get('groq_connection_status')
        if connection:
            import hashlib as _hashlib
            valid_for_current = (
                connection['model'] == st.session_state.agent_model
                and connection['key_hash'] == _hashlib.sha256(
                    get_api_key().encode('utf-8')).hexdigest())
            if valid_for_current:
                if connection['ok']:
                    st.success(connection['message'])
                else:
                    st.error(connection['message'])
            else:
                st.info('The API key or model has changed. Click Connect Groq / Test key again.')
        elif get_api_key():
            st.info('A Groq key is available. Click Connect Groq / Test key to verify the connection.')
        else:
            st.info('Paste your GroqCloud key above, then click Connect Groq / Test key.')
        if not st.session_state.get('agent_use_llm'):
            st.caption('Cloud AI is currently switched off. Turn it on after connecting if you want the assistant to use Groq.')
        st.caption('A successful connection is shown here. Entering a key alone does not call Groq. '
                   'The key is used only in your current session unless you configure it separately.')
        st.caption('Groq receives only the question, brief follow-up context, and diagnostics you explicitly ask it to explain—not the complete workbook. Check institutional data-sharing rules.')
        st.caption('Groq helps interpret unfamiliar task wording and choose a recommendation priority. '
                   'Python and independent validation decide all actual slots, conflicts and saved changes.')
    from timetable_assistant_features import show_feature
    if show_feature(st, st.session_state.assistant_feature, files, source):
        return
    with st.expander("How to use all four assistant features", expanded=False):
        st.markdown("""
1. **Chat & Tasks:** Ask about teachers, sections, rooms, conflicts, or workload. Say **Move [course] for [section]** to prepare a safe class update directly in this conversation. For the complete list of rooms, use **Find Free Slots → Available rooms**.
2. **Find Free Slots:** Choose **A class** and select its exact teaching session, then click **Find validated slots for this class**. Or choose **Available rooms** and filter day, period, room type and capacity.
3. **Shift a Class:** Enter the course and section (for example, *Shift CSL315 practical for BTECHCSE-V-AA*). Choose the exact session if asked, select a validated alternative, tick the coordinator approval box and click **Confirm and update timetable**. The app saves a revised SQLite version and reloads the corrected, highlighted timetable.
4. **Check Conflicts:** Choose a teaching session and proposed period, then click **Check conflicts**. This only checks/explains; it does **not** edit the timetable.

Timora uses source-grounded Python scheduling tools. Optional **GPT-OSS 120B (GroqCloud)** interprets unfamiliar phrasing; it does not invent timetable facts or approve edits. A two-period practical moves together in a compatible room. If no valid single-session move is found, nothing changes. Adding new teaching requirements or moving many other classes requires a fresh CP-SAT generation, not a one-class chat change.
        """)
    top_left, top_right = st.columns([1.7, 1], vertical_alignment="center")
    with top_left:
        st.markdown(f"**Timora conversation** · {sum(item.get('role') == 'user' for item in st.session_state.assistant_history)} question(s)")
        st.caption("Timora · verified scheduling tools · GroqCloud for supported language understanding")
    with top_right:
        st.caption('Groq writes regular chat answers from verified timetable facts when connected; local fallback is labeled.')
    history = st.session_state.assistant_history
    toolbar_left, toolbar_right = st.columns([2.6, 1], vertical_alignment="center")
    with toolbar_left:
        st.markdown('<div class="chat-subtle">Ask naturally about classes, teacher availability, workload, sections, free rooms, rules or possible moves. You can ask follow-up questions.</div>', unsafe_allow_html=True)
    with toolbar_right:
        if st.button('New chat', key='chat_new_conversation', use_container_width=True):
            st.session_state['assistant_chat_thread_id'] = None
            st.session_state['assistant_chat_force_new'] = True
            st.session_state['assistant_history'] = []
            st.session_state.pop('chat_move_state', None)
            st.rerun(scope='fragment')

    with st.expander('Chat history (saved locally)', expanded=False):
        st.caption('Chats stay on this laptop, grouped by workbook and timetable version. '
                   'Older timetable chats remain readable but cannot change the new timetable.')
        if available_chats:
            ids = [chat['id'] for chat in available_chats]
            by_id = {chat['id']: chat for chat in available_chats}
            chosen_id = st.selectbox('Previous conversations', ids,
                format_func=lambda cid: (('Current · ' if by_id[cid]['source'] == source_key else 'Older version · ')
                                         + by_id[cid]['title'] + ' · ' + by_id[cid]['updated'][:16]),
                key='chat_history_choice')
            same = by_id[chosen_id]['source'] == source_key
            open_col, delete_col = st.columns(2)
            with open_col:
                if st.button('Open chat', disabled=not same, key='chat_history_open',
                             use_container_width=True):
                    st.session_state['assistant_chat_thread_id'] = chosen_id
                    st.session_state['assistant_history'] = load_messages(digest, chosen_id)
                    st.session_state.pop('assistant_chat_force_new', None)
                    st.session_state.pop('chat_move_state', None)
                    st.rerun(scope='fragment')
            with delete_col:
                if st.button('Delete selected chat', key='chat_history_delete',
                             use_container_width=True):
                    delete_conversation(digest, chosen_id)
                    if st.session_state.get('assistant_chat_thread_id') == chosen_id:
                        st.session_state['assistant_chat_thread_id'] = None
                        st.session_state['assistant_chat_force_new'] = True
                        st.session_state['assistant_history'] = []
                        st.session_state.pop('chat_move_state', None)
                    st.session_state.pop('chat_history_choice', None)
                    st.rerun(scope='fragment')
            if not same:
                prior = load_messages(digest, chosen_id)
                st.info('This chat describes an older timetable. It is read-only.')
                with st.container(height=180, border=True):
                    for turn in prior[-12:]:
                        st.markdown(('**You:** ' if turn['role'] == 'user' else '**Timora:** ')
                                    + turn['content'])
        else:
            st.caption('No saved conversations for this workbook yet.')
        if st.session_state.get('assistant_chat_history_warning'):
            st.warning(st.session_state['assistant_chat_history_warning'])

    with st.container(height=470, border=False):
        if not history:
            st.info("Hi, I’m Timora. Ask about your timetable, or say ‘Move BCL203 for BCA-III-WDA’ to start a verified class update. I’ll find safe alternatives; only your approval publishes changes.")
        for index, item in enumerate(history):
            _render_chat_bubble(item["role"], item["content"], index)

    # A natural-language class-move request can be completed here, without
    # switching tabs. The same independent validator and coordinator approval
    # used by the existing Shift a Class feature remain mandatory.
    if source == 'active' and st.session_state.get('chat_move_state'):
        from timetable_assistant_features import show_chat_move_controls
        with st.expander('Complete class update', expanded=True):
            show_chat_move_controls(st, files, source)

    st.markdown('<div class="chat-sticky-form">', unsafe_allow_html=True)
    with st.form("coordinator_dialog_form_v7", clear_on_submit=True, border=False):
        question = st.text_input("Your question",
                                 placeholder="e.g. Is Manvi Bareja free on Tuesday at 11:30 AM?",
                                 key="chat_question_v7")
        submitted = st.form_submit_button("Send message", type="primary", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    with st.expander("Try a question or task", expanded=not history):
        suggestions = [
            ("Section schedule", "Show BTECHCSE-V-AB timetable on Tuesday"),
            ("Course teachers", "Who teaches CSL315?"),
            ("Teacher availability", "Is Aarti Kukreja free on Tuesday Period 3?"),
            ("Natural wording", "Is Manvi Breja maam available on Tuesday 11:30 am?"),
            ("Free rooms", "Which classrooms are free on Monday Period 3?"),
            ("Validation", "Show validation issues"),
            ("Move a class", "Move BCL203 theory for BCA-III-WDA"),
            ("Workload", "Which teacher has the most teaching periods this week?"),
            ("Section free time", "When is MCA-I-A free on Tuesday?"),
        ]
        for ix, (label, sample) in enumerate(suggestions):
            if st.button(label, key=f"dialog_suggestion_v7_{ix}", use_container_width=True):
                with st.spinner("Checking the timetable and preparing an answer..."):
                    reply = _answer_question(sample, files, source_label=source_label, comparison_files=comparison_files)
                _record_assistant_turns([
                    {"role": "user", "content": sample},
                    {"role": "assistant", "content": reply},
                ])
                st.rerun(scope="fragment")

    if source == 'active':
        with st.expander('Prepare a class-move request', expanded=False):
            st.caption('The assistant records a request against this selected timetable. '
                       'It does not move any class. Open Results → Coordinator workflows to check '
                       'conflicts and approve or reject the proposal.')
            try:
                from timetable_rescheduling import csv_rows, timetable_sessions
                from timetable_records import submit_request
                from timetable_workflows_ui import display_session, slot_label
                from northcap_cp_sat_solver import excel_records, clean
                current_rows = csv_rows(files['timetable_all.csv'])
                class_sessions = timetable_sessions(current_rows)
                workbook_data = excel_records(io.BytesIO(st.session_state.workbook_bytes))
                starts = sorted(workbook_data['Time_Slots'],
                                key=lambda r: (clean(r['day']), int(float(r['period']))))
                with st.form('assistant_move_request_v116'):
                    role = st.selectbox('Demo request from', ['Teacher', 'Student'],
                                        help='Demonstration roles only; not real university accounts.')
                    sid = st.selectbox('Teaching session to move', sorted(class_sessions),
                                       format_func=lambda k: display_session(class_sessions[k]))
                    slot = st.selectbox('Preferred new start period',
                                        [clean(r['slot_id']) for r in starts],
                                        format_func=lambda slot_id: next(
                                            (slot_label(r) for r in starts if clean(r['slot_id']) == slot_id), slot_id))
                    reason = st.text_area('Reason for the request', max_chars=600,
                                          placeholder='Example: Teacher prefers a different Tuesday period')
                    requested = st.form_submit_button('Send request for coordinator review',
                                                       type='primary', use_container_width=True)
                if requested:
                    if not reason.strip():
                        st.error('Please enter a reason for the change.')
                    else:
                        first = class_sessions[sid][0]
                        source_label = ('Demo on behalf of ' + clean(first['faculty_name'])
                                        if role == 'Teacher' else
                                        'Demo group ' + clean(first['group_id']))
                        try:
                            request_id = submit_request(digest, files['timetable_all.csv'],
                                                        role, source_label, sid, slot, reason)
                            st.success(f'Request #{request_id} is pending. No timetable was changed. '
                                       'Open Coordinator workflows to check feasibility and approve or reject.')
                        except Exception as exc:
                            st.error('Request was not submitted: ' + str(exc))
            except Exception as exc:
                st.warning('Class-move request form is unavailable: ' + str(exc))

    if submitted and question and question.strip():
        question = question.strip()
        with st.spinner("Checking the timetable and preparing an answer..."):
            move_handled = False
            reply = None
            if source == 'active':
                from timetable_chat_moves import route_move_message
                from timetable_rescheduling import csv_rows
                from northcap_cp_sat_solver import excel_records
                chat_rows = csv_rows(files['timetable_all.csv'])
                chat_data = excel_records(io.BytesIO(st.session_state.workbook_bytes))
                move_handled, reply, move_state = route_move_message(
                    question, chat_rows, chat_data,
                    st.session_state.get('chat_move_state'),
                    include_mentoring=result.get('include_mentor', True),
                    extra_labs=result.get('sim_count', 0),
                    llm_enabled=st.session_state.get('agent_use_llm', False),
                    model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
                if move_handled:
                    st.session_state['chat_move_state'] = move_state
                    st.session_state['assistant_move_reply'] = reply + (
                        '\n\nUse **Complete class update** below to choose a validated slot and approve it.'
                        if move_state and move_state.get('stage') == 'options' else '')
                    reply = st.session_state['assistant_move_reply']
            if not move_handled:
                reply = _answer_question(question, files, source_label=source_label, comparison_files=comparison_files)
        _record_assistant_turns([
            {"role": "user", "content": question},
            {"role": "assistant", "content": reply},
        ])
        st.rerun(scope="fragment")


def results_page() -> None:
    # Clear an obsolete version choice BEFORE constructing any Streamlit widgets.
    # Saved working copies can have different option labels from the prior run.
    if st.session_state.pop('reset_timetable_variant_on_next_run', False):
        st.session_state.pop('timetable_variant', None)
        st.session_state.pop('previous_timetable_variant', None)
    result = st.session_state.solver_result
    if result is None:
        st.markdown('<div class="workspace-banner"><span class="section-eyebrow">RESULTS</span>'
                    '<h1>Your timetable results will appear here.</h1>'
                    '<p>Generate a schedule in the workspace to browse and download the output.</p></div>', unsafe_allow_html=True)
        if st.button("Open Workspace  →", type="primary"):
            go("studio")
        return

    rc, files = result["status_code"], result["files"]
    st.markdown('<div class="workspace-banner"><span class="section-eyebrow">TIMETABLE RESULTS</span>'
                '<h1>Your scheduling command centre.</h1>'
                '<p>Review verified timetables, compare quality, inspect policies, track saved generations and consult Timora.</p></div>', unsafe_allow_html=True)

    from timetable_identity import safe_workbook_label
    source_name = safe_workbook_label(result.get('input_file') or st.session_state.workbook_name)
    active_institution = result.get('university_name') or st.session_state.get('university_name') or 'Not specified'
    st.markdown('<div class="note-card"><b>Excel workbook:</b> '
                + html.escape(source_name) + '<br><b>University:</b> '
                + html.escape(active_institution) + '</div>', unsafe_allow_html=True)

    success = rc == 0 and "timetable_sections.html" in files and "timetable_all.csv" in files
    if not success:
        attempted_kind = 'fairness' if result.get('fairness_requested') else 'standard'
        st.markdown('<div class="error-banner"><b>No validated '
                    + attempted_kind + ' timetable was generated.</b><br>'
                    'Review the diagnostic report below. No unverified timetable will be published.</div>',
                    unsafe_allow_html=True)
        if (result.get('baseline_independent_status') in ('PASS', 'PASS_WITH_LIMITATIONS')
                and 'baseline_timetable_sections.html' in files):
            st.info('The separately validated STANDARD timetable is still available. '
                    'The fairness optimization did not finish successfully under the current settings.')
            cols = st.columns(2)
            cols[0].download_button('Download valid standard timetable (HTML)',
                                    files['baseline_timetable_sections.html'],
                                    file_name='standard_timetable.html', mime='text/html')
            if 'baseline_timetable_all.csv' in files:
                cols[1].download_button('Download valid standard timetable (CSV)',
                                        files['baseline_timetable_all.csv'],
                                        file_name='standard_timetable.csv', mime='text/csv')
            if 'baseline_timetable_all.csv' in files and st.session_state.workbook_bytes:
                from timetable_versions import save_explicit_version
                from timetable_records import list_versions, data_digest
                from timetable_rescheduling import schedule_hash
                digest = data_digest(st.session_state.workbook_bytes)
                saved = {v['schedule_hash'] for v in list_versions(digest)}
                already = schedule_hash(files['baseline_timetable_all.csv']) in saved
                if st.button('Save validated Standard to history',
                             disabled=already, key='save_failed_comparison_baseline'):
                    try:
                        version_id, _ = save_explicit_version(
                            st.session_state.workbook_bytes, result, 'STANDARD')
                        st.success(f'Standard timetable saved as {version_id}.')
                        st.rerun()
                    except Exception as exc:
                        st.error('Standard was not saved: ' + str(exc))
                if already:
                    st.caption('This Standard timetable has already been saved.')
        text = files.get("validation_report.txt", b"").decode("utf-8", errors="replace")
        if text:
            with st.expander("Validation report", expanded=True):
                st.code(text[-18000:], language="text")
            st.download_button("Download validation report", files["validation_report.txt"],
                               file_name="validation_report.txt", mime="text/plain")
        if "policy_compliance.txt" in files:
            with st.expander("Institution policy audit", expanded=True):
                st.code(files["policy_compliance.txt"].decode("utf-8", errors="replace")[-22000:], language="text")
            st.download_button("Download policy audit", files["policy_compliance.txt"],
                               file_name="policy_compliance.txt", mime="text/plain")
        with st.expander("CP-SAT solver log", expanded=True):
            st.code(result.get("logs", "")[-18000:] or "(No solver log)", language="text")
        if st.button("Return to Workspace", key="retry_solver"):
            go("studio")
        return

    if result.get('rescheduled') and st.session_state.get('assistant_last_published'):
        st.success(st.session_state['assistant_last_published'] +
                   ' The Updated Timetable is loaded below; gold cards mark moved classes.')
    if result.get('rescheduled'):
        st.info('**Updated Timetable** — the class change passed full independent validation. '
                'The earlier version is preserved in local history; gold highlights mark moved classes.')
        revised_cols = st.columns(2)
        revised_cols[0].download_button(
            'Download Updated Timetable (HTML)', files['timetable_sections.html'],
            file_name='updated_timetable.html', mime='text/html',
            key='download_updated_html', use_container_width=True)
        revised_cols[1].download_button(
            'Download Updated Timetable (CSV)', files['timetable_all.csv'],
            file_name='updated_timetable.csv', mime='text/csv',
            key='download_updated_csv', use_container_width=True)
    elif result.get('loaded_saved_version'):
        st.info(f"Working copy of saved version {result['loaded_saved_version']}. "
                'The archived original has not been changed. Request a class move through Timora '
                'or Coordinator workflows; a validated revision requires approval.')
    checked = result.get("independent_status", "NOT_RUN")
    if result.get("fell_back_to_standard"):
        st.info("The standard timetable is independently validated and available below. "
                + result.get("fallback_reason", "Fairness search did not improve it.")
                + " No fairness improvement is claimed for this run.")
    if result.get("sim_count"):
        st.warning("Simulation result: extra fictional laboratories were added in memory for this run. "
                   "Do not treat them as verified university rooms.")
    if checked == "PASS_WITH_LIMITATIONS":
        st.markdown('<div class="result-banner"><h2>Timetable generated and independently checked</h2>'
                    '<p>All implemented hard checks passed. Synthetic room/faculty records and individual student enrolments still require verification.</p></div>',
                    unsafe_allow_html=True)
    elif checked == "FAIL":
        st.error("The solver produced a timetable, but the separate post-solve validator detected conflicts. "
                 "Do not treat this as an approved schedule. See Validation & log.")
    else:
        st.warning("The solver produced a timetable but the independent validator did not complete. "
                   "Review Validation & log before trusting the output.")

    rows = list(csv.DictReader(io.StringIO(files["timetable_all.csv"].decode("utf-8-sig", errors="replace"))))
    report = files["timetable_sections.html"].decode("utf-8", errors="replace")
    sections = [html.unescape(x) for x in re.findall(
        r'<section class="section-card timetable-panel" id="section-\d+" data-view="section" data-group="([^"]+)"', report)]
    metrics = st.columns(4)
    for col, label, count in zip(
        metrics,
        ["Academic sections", "Scheduled sessions", "Courses", "Teaching faculty"],
        [len(sections), len({r["session_id"] for r in rows if r.get("session_id")}),
         len({r["course_id"] for r in rows if r.get("course_id")}),
         len({r["faculty_name"] for r in rows if r.get("faculty_name")})]
    ):
        col.metric(label, count)

    try:
        from timetable_records import data_digest, list_versions
        archive_versions = list_versions(data_digest(st.session_state.workbook_bytes)) if st.session_state.workbook_bytes else []
    except Exception:
        archive_versions = []
    from timetable_versions import available_versions
    save_options = available_versions(result)

    with st.container():
        tab_timetable, tab_dashboard, tab_quality, tab_policy, tab_workflows, tab_history, tab_exports, tab_validation = st.tabs([
            "Timetable explorer", "Analytics dashboard", "Standard vs Fairness", "Institution policies",
            "Coordinator workflows", "Saved runs", "Downloads", "Validation & log"])
        with tab_timetable:
            # Keep the schedule iframe and Save/Unsave actions above these controls.
            # Widget values arrive in session_state at the start of each Streamlit rerun,
            # so we can compute the active timetable before rendering its selectors.
            from timetable_versions import context_result
            variants = [o['label'] for o in save_options]
            if not variants:
                st.error('No independently verified timetable can be previewed.')
                return
            selected_variant = st.session_state.get('timetable_variant')
            if selected_variant not in variants:
                # Show the most recently generated result (Fairness, if it
                # exists) rather than silently selecting the Standard baseline.
                selected_variant = variants[-1]
                st.session_state['timetable_variant'] = selected_variant
            if st.session_state.get('previous_timetable_variant') != selected_variant:
                st.session_state.previous_timetable_variant = selected_variant
                st.session_state.assistant_history = []
                st.session_state.pop('chat_move_state', None)
                st.session_state.coordinator_preview = None
            active_view = context_result(result, selected_variant)
            current_html = active_view['files']['timetable_sections.html'].decode('utf-8', errors='replace')
            # The HTML report already provides section-wise and faculty-wise
            # search and navigation in its own sidebar. The external radio and
            # dropdown controls duplicated that UI and are intentionally removed.
            # Keep the coordinator's pending jump to a particular section.
            pending_section = st.session_state.pop('pending_preview_section', None)
            if pending_section in sections:
                encoded = json.dumps(pending_section).replace('<', '\\u003c')
                script = ("<script>window.addEventListener('load', function(){"
                          f"if(window.focusTimetableGroup)window.focusTimetableGroup({encoded});"
                          "});</script>")
                preview = current_html.replace('</body>', script + '</body>')
            else:
                preview = current_html
            st.components.v1.html(preview, height=1000, scrolling=True)
            st.caption('Section views can show parallel batches; faculty and room views show '
                       'scheduled occupancy from the same selected timetable. '
                       'An empty room period alone does not prove a class move is feasible.')

            # These actions belong directly under the timetable being viewed,
            # not in a separate global row above the results navigation.
            from timetable_rescheduling import schedule_hash
            from timetable_versions import active_version, save_explicit_version
            from timetable_records import data_digest, unsave_manual_version
            chosen = active_version(result, selected_variant)
            chosen_hash = schedule_hash(chosen['csv'])
            saved_match = next((v for v in archive_versions
                                if v['schedule_hash'] == chosen_hash), None)
            st.markdown('#### Save or remove this timetable')
            st.caption(f"Selected: **{chosen['label']}**. "
                       "The original Excel input is never changed. Saving keeps a local history copy; "
                       "downloading creates a CSV you can open in Excel.")
            if st.session_state.get('last_manual_save_message'):
                st.success(st.session_state.last_manual_save_message)
            download_col, save_col, unsave_col = st.columns(3)
            with download_col:
                st.download_button('Download selected CSV', chosen['csv'],
                                   file_name=f"{chosen['kind'].lower()}_timetable.csv",
                                   mime='text/csv', use_container_width=True,
                                   key=f"view_download_{chosen['kind']}")
            with save_col:
                can_save = chosen['kind'] in ('STANDARD', 'FAIRNESS') and saved_match is None
                if st.button('Save this timetable', use_container_width=True,
                             type='primary' if can_save else 'secondary',
                             disabled=not can_save,
                             key=f"view_save_{chosen['kind']}"):
                    try:
                        with st.spinner('Validating and saving the selected timetable...'):
                            version_id, _ = save_explicit_version(
                                st.session_state.workbook_bytes, result, chosen['kind'])
                        st.session_state.last_manual_save_message = (
                            f'{chosen["label"]} saved in local history as {version_id}.')
                        st.rerun()
                    except Exception as exc:
                        st.error('Could not save: ' + str(exc))
            with unsave_col:
                can_unsave = (saved_match is not None and
                              saved_match['variant'] in ('STANDARD', 'FAIRNESS'))
                if st.button('Unsave this timetable', use_container_width=True,
                             disabled=not can_unsave,
                             key=f"view_unsave_prepare_{chosen_hash[:12]}"):
                    st.session_state['confirm_unsave_hash'] = chosen_hash
            if saved_match:
                st.caption(f"Saved in history as version **{saved_match['version_id']}** "
                           f"({saved_match['variant']}).")
                if not can_unsave:
                    st.info('This is a coordinator revision/audit record and cannot be unsaved.')
            else:
                st.caption('This view is not archived yet; only independently validated results are automatically saved.')
            if can_unsave and st.session_state.get('confirm_unsave_hash') == chosen_hash:
                st.warning('Unsave removes this version from local history. It does not delete any CSV already downloaded. '
                           'Versions linked to change requests or approvals are protected.')
                confirm_col, cancel_col = st.columns(2)
                with confirm_col:
                    if st.button('Confirm unsave', key=f"view_unsave_confirm_{chosen_hash[:12]}",
                                 use_container_width=True):
                        try:
                            unsave_manual_version(data_digest(st.session_state.workbook_bytes),
                                saved_match['version_id'], expected_schedule_hash=chosen_hash)
                            st.session_state.pop('confirm_unsave_hash', None)
                            st.session_state.last_manual_save_message = 'The selected timetable was unsaved from local history.'
                            st.rerun()
                        except ValueError as exc:
                            st.error('Cannot unsave: ' + str(exc))
                with cancel_col:
                    if st.button('Cancel', key=f"view_unsave_cancel_{chosen_hash[:12]}",
                                 use_container_width=True):
                        st.session_state.pop('confirm_unsave_hash', None)
                        st.rerun()
            st.markdown('<div class="premium-inline-note">To request a class change, open <b>Timora → Prepare a class-move request</b> or <b>Coordinator workflows</b>. '
                        'A change creates a new validated CSV and updated timetable report only after approval; it never overwrites the original Excel workbook.</div>',
                        unsafe_allow_html=True)

            # A version picker is still necessary when both Standard and
            # Fairness exist; the HTML report itself handles faculty/section
            # browsing. Hide the picker entirely for a single-version run.
            if len(variants) > 1:
                st.radio('Choose timetable version', variants, horizontal=True,
                         key='timetable_variant',
                         help='Timora and coordinator workflows use the selected timetable version.')

            # Fairness is deliberately located BELOW the visible timetable and
            # save controls, never on the Home or Standard-generation page.
            # The button only requests a separate seeded Fairness CP-SAT run.
            st.markdown('---')
            st.markdown('### Fairness-optimized timetable (optional)')
            standard_ready = verified_standard_ready()
            if standard_ready:
                st.caption('Optional: improve the generated Standard timetable. '
                           'Click below to run Fairness separately; Standard will not run again.')
            else:
                st.info('Fairness requires a validated Standard timetable as its starting point. '
                        'If you reopened a saved Fairness or coordinator version, restore its saved '
                        'Standard baseline below; you do not need to generate Standard again.')
                if st.session_state.get('workbook_bytes') and st.session_state.get('workbook_digest'):
                    from timetable_records import list_versions as _list_saved_versions
                    saved_standards = [item for item in _list_saved_versions(
                        st.session_state.workbook_digest) if item['variant'] == 'STANDARD']
                    if saved_standards:
                        if st.button('Restore saved Standard for Fairness',
                                     key='restore_standard_for_alns'):
                            try:
                                with st.spinner('Revalidating saved Standard timetable...'):
                                    if restore_standard_baseline_from_archive():
                                        st.rerun()
                                    else:
                                        st.error('Saved Standard workbook data is unavailable. '
                                                 'Open the saved Standard version from Home first.')
                            except (OSError, ValueError, KeyError, TypeError, __import__('sqlite3').DatabaseError) as restore_error:
                                st.error('Could not restore Standard baseline: ' + str(restore_error))
                    else:
                        st.caption('No saved Standard version exists for this workbook. '
                                   'Generate Standard once, then return here to enable UCB1.')
            # The coordinator can select a fairness focus manually. UCB1
            # neighborhood learning remains available as a separate solve mode.
            from timetable_fairness import (FAIRNESS_PROFILE_LABELS,
                                            available_focuses_from_policy_report)
            baseline_policy_file = (st.session_state.standard_result.get('files', {}).get('policy_compliance.json')
                                    if standard_ready else None)
            try:
                policy_info = json.loads(baseline_policy_file.decode('utf-8')) if baseline_policy_file else {}
            except (ValueError, UnicodeError, AttributeError):
                policy_info = {}
            allowed_fairness_focuses = available_focuses_from_policy_report(policy_info)
            if st.session_state.get('results_fairness_focus', 'balanced') not in allowed_fairness_focuses:
                st.session_state['results_fairness_focus'] = 'balanced'
            # Independent locally implemented ML: UCB1 online learning from
            # previously measured, independently validated Fairness runs.
            st.markdown('**Choose Fairness mode**')
            st.checkbox('Use UCB1 adaptive fairness (recommended)',
                        value=False, key='results_fairness_alns', disabled=not standard_ready,
                        help='UCB1 learns which local search neighborhoods to try. All timetable changes are verified by CP-SAT and the independent validator.')
            if st.session_state.get('results_fairness_alns', False):
                st.caption('Mode: **UCB1-guided Fairness** — automatically tries validated improvements to the Standard timetable.')
            else:
                st.caption('Mode: **Manual CP-SAT Fairness** — uses the selected fairness focus and search settings.')
            prefs = st.session_state.get('quality_saved', {
                'optimize': True, 'compare': False,
                'priority': 'Protect policy and late periods first'})
            st.caption('Advanced options are optional; open Timetable Quality settings only when needed.')
            with st.expander('Timetable Quality settings', expanded=False):
                if 'results_fairness_compare' not in st.session_state:
                    st.session_state['results_fairness_compare'] = bool(prefs.get('compare', False))
                if 'results_fairness_priority' not in st.session_state:
                    st.session_state['results_fairness_priority'] = prefs.get(
                        'priority', 'Protect policy and late periods first')
                # The separate Standard vs Fairness dashboard now always displays
                # verified before/after metrics. Do not make this a second opt-in.
                st.session_state['results_fairness_compare'] = True
                st.caption('Before/after comparison is automatic in the Standard vs Fairness tab.')
                st.selectbox('Manual fairness policy priority',
                             ['Protect policy and late periods first', 'Balanced weights from Excel'],
                             key='results_fairness_priority')
                st.caption('This priority selection applies to manual Fairness. UCB1 '
                           'uses measured fairness as its objective with separate '
                           'Standard-baseline policy safeguards.')
                if 'section_gaps' not in allowed_fairness_focuses:
                    st.caption('Section-gap optimization requires the Excel soft policy '
                               '`minimize_student_gaps` to be enabled. Other fairness focuses remain available.')
                st.selectbox('Fairness objective focus',
                             options=list(allowed_fairness_focuses),
                             format_func=lambda focus: FAIRNESS_PROFILE_LABELS[focus],
                             key='results_fairness_focus',
                             help='The selected focus changes CP-SAT fairness weights and which '
                                  'validated sessions are reconsidered. Balanced keeps V18.17 behavior.')
                st.caption('Advanced search size controls which sessions CP-SAT may reconsider, '
                           'not the number of changes accepted. University hard constraints always remain active.')
                if st.session_state.get('results_fairness_alns', False):
                    # Adaptive search starts with small groups of classes
                    # and widens as needed, within the existing 180s budget.
                    st.session_state['results_fairness_fraction'] = 35
                    st.session_state['results_fairness_seconds'] = 180
                    st.caption('UCB1 manages the search automatically (180 s budget); only validated improvements are retained.')
                else:
                    st.select_slider(
                        'Teaching sessions allowed to move within search (not final change limit)',
                        options=[20, 35, 50], value=35,
                        key='results_fairness_fraction', format_func=lambda v: f'{v}%')
                    st.slider('Fairness search time limit (seconds)',
                        min_value=60, max_value=600, value=180, step=30,
                        key='results_fairness_seconds')
            chosen_focus = st.session_state.get('results_fairness_focus', 'balanced')
            from timetable_fairness import weights_for_profile
            mode_text = ('UCB1 adaptive search' if st.session_state.get('results_fairness_alns', False)
                         else 'Manual CP-SAT Fairness')
            st.caption('Run plan: ' + mode_text + ' · Focus: ' + FAIRNESS_PROFILE_LABELS[chosen_focus])
            with st.expander('Objective weights used for this run', expanded=False):
                st.json(weights_for_profile(chosen_focus))
                st.caption('These are predefined fairness coefficients; university hard constraints remain unchanged.')
            # Actual UCB1 neighborhood-learning evidence persists across browser reloads.
            if standard_ready:
                try:
                    from timetable_bandit_alns import (learning_context,
                        operator_statistics, learning_history, validate_change_costs)
                    from timetable_records import data_digest
                    # Match the live UCB1 settings. Legacy capped experiments
                    # remain separate in SQLite and are not mixed into this mode.
                    alns_context = learning_context(
                        seconds=int(st.session_state.get('results_fairness_seconds', 180)),
                        fraction=float(st.session_state.get('results_fairness_fraction', 35)) / 100.,
                        profile=chosen_focus,
                        priority='guarded_fairness',
                        include_mentor=st.session_state.standard_result.get('include_mentor', True),
                        extra_labs=st.session_state.standard_result.get('sim_count', 0),
                        change_costs=validate_change_costs())
                    dataset_hash = data_digest(st.session_state.workbook_bytes)
                    with st.expander('Review UCB1 neighborhood learning and saved experiment history', expanded=False):
                        st.dataframe(operator_statistics(dataset_hash, alns_context),
                                     hide_index=True, use_container_width=True)
                        previous_rounds = learning_history(dataset_hash, alns_context)
                        if previous_rounds:
                            st.caption('Real validated neighborhood-search observations saved in SQLite. '
                                       'A failed or unaccepted round earns zero reward.')
                            evidence_stream = io.StringIO()
                            evidence_writer = csv.DictWriter(evidence_stream,
                                fieldnames=list(previous_rounds[0]))
                            evidence_writer.writeheader()
                            evidence_writer.writerows(previous_rounds)
                            st.download_button('Download all UCB1 neighborhood experiments (CSV)',
                                               data=evidence_stream.getvalue(),
                                               file_name='timora_ucb1_neighborhood_history.csv',
                                               mime='text/csv', key='alns_saved_history')
                        else:
                            st.caption('No neighborhood experiments for these settings yet. '
                                       'Enable UCB1 adaptive fairness and run Fairness once.')
                except (OSError, ValueError, TypeError, __import__('sqlite3').DatabaseError) as exc:
                    st.caption('Saved neighborhood-learning history unavailable: ' + str(exc))
            st.session_state['quality_saved'] = {
                'optimize': True,
                'compare': True,
                'priority': st.session_state.get('results_fairness_priority',
                                               'Protect policy and late periods first'),
            }
            st.caption('Standard = blue · Fairness = green. Open Results → Standard vs Fairness to compare results after generation.')
            # Streamlit keeps widget values in session state even when the expander
            # is closed. Always use the validated baseline's *stored* mentor and
            # laboratory settings rather than the Workspace's current defaults.
            if st.button('Generate Fairness-Optimized Timetable  →',
                         disabled=not standard_ready, key='run_fairness_only',
                         type='primary' if standard_ready else 'secondary',
                         use_container_width=True):
                standard = st.session_state.standard_result
                strategy = ('policy_first' if st.session_state['quality_saved']['priority'].startswith('Protect')
                            else 'balanced')
                run_solver(
                    seconds=int(st.session_state.get('results_fairness_seconds', 180)),
                    include_mentor=standard.get('include_mentor', True),
                    extra_labs=standard.get('sim_count', 0),
                    optimize_fairness=True,
                    compare_standard=st.session_state['quality_saved']['compare'],
                    fairness_priority=strategy,
                    neighborhood_fraction=float(st.session_state.get('results_fairness_fraction', 35)) / 100.0,
                    fairness_profile=chosen_focus,
                    adaptive_neighborhoods=bool(st.session_state.get('results_fairness_alns', False)),
                )
            alns_file = result.get('files', {}).get('alns_learning_report.json')
            if result.get('fairness_requested') and alns_file:
                try:
                    alns = json.loads(alns_file)
                    st.markdown('#### UCB1 AI Fairness — Verified Results')
                    audit = alns.get('final_change_audit') or {}
                    improvement = max(0., float(alns.get('relative_improvement', 0.)) * 100)
                    original = int(alns.get('starting_balanced_score', 0))
                    final = int(alns.get('best_balanced_score', original))
                    attempts = int(alns.get('attempted', 0))
                    successes = int(alns.get('improved', 0))
                    total = int(audit.get('total_sessions', 0))
                    changed = int(audit.get('distinct_changed_sessions', 0))
                    if successes == 0:
                        st.warning('No validated improvement in this run. The original Standard '
                                   'timetable was preserved. This does not indicate that the '
                                   'university timetable failed.')
                    else:
                        st.success('A validated fairness improvement was found. The best '
                                   'accepted timetable was kept without a preset percentage target.')
                    cards = st.columns(4)
                    cards[0].metric('Measured fairness improvement', f'{improvement:.2f}%')
                    cards[1].metric('Distinct sessions changed', f'{changed} / {total}')
                    cards[2].metric('Improvements accepted', f'{successes} / {attempts}')
                    cards[3].metric('Elapsed search time', f"{alns.get('elapsed_seconds',0):.1f} s")
                    st.caption(f'Measured fairness penalty: {original:,} → {final:,} '
                               '(lower is better). Each accepted result passed independent validation.')
                    if total:
                        st.caption(f'Timetable stability: {total-changed:,} of {total:,} '
                                   f'sessions retained their original placements '
                                   f'({(total-changed)/total*100:.1f}% unchanged).')
                    st.markdown('##### What changed in the final timetable?')
                    change_cols = st.columns(3)
                    change_cols[0].metric('Time changes', audit.get('time_changed_sessions', 0))
                    change_cols[1].metric('Room-only changes', audit.get('room_only_changed_sessions', 0))
                    change_cols[2].metric('Practical blocks changed', audit.get('practical_sessions_changed', 0))
                    st.caption('A two-period practical is counted once, not twice. A class moved '
                               'across several rounds is also counted once in this final audit. '
                               f"Weighted change cost: {audit.get('weighted_disruption_cost', 0)}. "
                               'This is an illustrative cost, not an institutional rule.')
                    if alns.get('final_changed_sessions'):
                        with st.expander('View changed classes and their new placements'):
                            rows = alns['final_changed_sessions']
                            st.dataframe([{'Class / Course': r.get('course_id',''),
                                           'Section': r.get('group_id',''),
                                           'Type': r.get('session_kind',''),
                                           'Change': r.get('change_type','').replace('_',' ').title(),
                                           'Original': r.get('standard_placement',''),
                                           'Updated': r.get('final_placement','')}
                                          for r in rows], use_container_width=True, hide_index=True)
                    st.download_button('Download final class changes (CSV)',
                                       data=result['files'].get('alns_final_changes.csv', b''),
                                       file_name='timora_ucb1_final_changes.csv',
                                       mime='text/csv', key='download_ucb1_final_changes')
                    # Show readable summaries first; retain the full numerical
                    # experiment records for reproducibility as downloads.
                    st.markdown('##### How UCB1 improved this timetable')
                    display_rounds = []
                    for entry in alns.get('rounds', []):
                        display_rounds.append({
                            'Round': entry.get('round'),
                            'Class repair strategy': entry.get('label', entry.get('operator','')),
                            'Outcome': 'Accepted' if entry.get('accepted') else 'Not accepted',
                            'Penalty before': entry.get('previous_score'),
                            'Penalty after': entry.get('candidate_score'),
                            'Time (s)': entry.get('elapsed_seconds'),
                        })
                    if display_rounds:
                        st.dataframe(display_rounds, use_container_width=True, hide_index=True)
                    else:
                        st.caption('No improvement attempts were recorded for this saved result.')
                    if successes == 0 and attempts:
                        with st.expander('Why were the UCB1 attempts not accepted?', expanded=True):
                            st.dataframe([{'Round': r.get('round'),
                                           'Strategy': r.get('label'),
                                           'Reason': r.get('reason'),
                                           'Solver exit': r.get('solver_exit_code'),
                                           'Validation': r.get('independent_status')}
                                          for r in alns.get('rounds', [])],
                                         use_container_width=True, hide_index=True)
                            st.caption('A zero result means the original Standard was kept; '
                                       'it does not mean the original timetable failed.')
                    with st.expander('Technical evidence and fairness–stability history'):
                        st.caption('Every accepted result passed the independent validator. '
                                   'Changes are compared against the original Standard timetable; '
                                   'smaller weighted costs indicate less disruption.')
                        points = alns.get('observed_tradeoffs', [])
                        if points:
                            st.dataframe([{
                                'Round': p.get('round'),
                                'Fairness improvement (%)': p.get('improvement_percent'),
                                'Sessions changed': p.get('distinct_changed_sessions'),
                                'Weighted change cost': p.get('weighted_disruption_cost')}
                                for p in points], use_container_width=True, hide_index=True)
                            output = io.StringIO()
                            writer = csv.DictWriter(output, fieldnames=list(points[0]))
                            writer.writeheader()
                            writer.writerows(points)
                            st.download_button('Download fairness–stability evidence (CSV)',
                                data=output.getvalue().encode('utf-8-sig'),
                                file_name='timora_fairness_stability.csv', mime='text/csv',
                                key='download_fairness_stability')
                        st.caption('Strategy outcomes use online-learning rewards. '
                                   'Raw fields are preserved in the experiment CSV.')
                        st.dataframe(alns.get('rounds', []), use_container_width=True,
                                     hide_index=True)
                    st.download_button('Download UCB1 experiment evidence (CSV)',
                                       data=result['files'].get('alns_learning_rounds.csv', b''),
                                       file_name='timora_ucb1_alns_rounds.csv',
                                       mime='text/csv', key='download_ucb1_alns')
                    validation_state = alns.get('final_validation_status',
                                                result.get('independent_status'))
                    if validation_state == 'PASS_WITH_LIMITATIONS':
                        st.caption('Validation: modeled scheduling checks passed with documented '
                                   'dataset limitations. See Validation & log for details.')
                    elif validation_state == 'PASS':
                        st.caption('Validation: independently checked against modeled constraints.')
                    st.caption('UCB1 experiments optimize validated fairness while softly '
                               'penalizing changes to the original timetable. Improvements '
                               'are measured, not predicted or guaranteed.')
                except (ValueError, KeyError, TypeError):
                    st.warning('The UCB1 experiment summary could not be displayed; '
                               'review the downloadable validation log.')
            if result.get('fairness_requested') and result.get('fell_back_to_standard'):
                st.warning('Fairness was attempted, but no independently validated improvement was '
                           'published. The Standard timetable remains available. '
                           'Review the technical experiment evidence before retrying.')

        with tab_dashboard:
            from timetable_dashboard import render_analytics_dashboard
            same_dataset = bool(
                st.session_state.get('workbook_digest')
                and st.session_state.get('standard_workbook_digest')
                and st.session_state.workbook_digest == st.session_state.standard_workbook_digest)
            render_analytics_dashboard(
                st, result, st.session_state.get('standard_result'),
                same_workbook=same_dataset,
                archive_versions=archive_versions,
                workbook_blob=st.session_state.get('workbook_bytes'),
                workbook_name=st.session_state.get('workbook_name') or 'university.xlsx',
                university_name=active_institution)

        with tab_quality:
            from timetable_dashboard import render_fairness_dashboard
            render_fairness_dashboard(
                st, result, st.session_state.get('standard_result'),
                same_workbook=same_dataset,
                archive_versions=archive_versions,
                workbook_blob=st.session_state.get('workbook_bytes'),
                workbook_name=st.session_state.get('workbook_name') or 'university.xlsx',
                university_name=active_institution)
            st.markdown("### Fairness optimization details")
            if "quality_summary.json" not in files:
                st.info("No finished quality report is available. Generate and validate a timetable first.")
            else:
                quality = json.loads(files["quality_summary.json"].decode("utf-8"))
                q = quality["metrics"]
                st.caption("These are computed from your GENERATED timetable, not predicted values. Lower values are generally preferable.")
                qcols = st.columns(4)
                qcols[0].metric("Faculty gap periods", q["teacher_gap_periods"])
                qcols[1].metric("Section avoidable gaps", q["section_avoidable_gap_proxy"])
                qcols[2].metric("Daily overload periods", q["teacher_daily_overload_periods"])
                qcols[3].metric("Soft-avoid occupied periods", q.get("preferred_free_slot_periods", q["thursday_afternoon_session_periods"]))
                st.caption("Section gaps are a conservative proxy based on parent sections and their named batches; one midday break is excluded. Mixed elective enrolment is not fully modeled without student IDs.")
                if result.get('fairness_requested'):
                    profile = result.get('fairness_profile', 'balanced')
                    from timetable_fairness import FAIRNESS_PROFILE_LABELS
                    st.caption('Fairness search focus: ' + FAIRNESS_PROFILE_LABELS.get(profile, profile) +
                               ' · selected by coordinator/default' +
                               (' · Standard baseline retained (no validated improvement)' if result.get('fell_back_to_standard')
                                else ' · optimized result published'))

                st.markdown("**Optimization used:** " + (
                    "Saved timetable working copy (no fresh optimization; original placements retained)" if result.get('loaded_saved_version')
                    else "UCB1 guarded fairness: optimize measured fairness while protecting Standard policy/late-period caps" if result.get("fairness_enabled") and result.get("fairness_priority") == "guarded_fairness"
                    else "Priority protected: Excel soft-avoid slots → late periods → fairness gaps" if result.get("fairness_enabled") and result.get("fairness_priority") == "policy_first"
                    else "Balanced fairness with Excel soft penalties" if result.get("fairness_enabled")
                    else "Coordinator-approved class update (validated)" if result.get("rescheduled")
                    else "Standard original CP-SAT soft penalties"))
                if result.get("comparison_guards") is not None and not result.get("fell_back_to_standard"):
                    guards = result["comparison_guards"]
                    st.success(f"Baseline guardrails active: no more than {guards['avoid']} soft-avoid occupied periods "
                               f"and {guards['late']} late teaching periods. Actual output was independently checked.")
                if result.get("comparison"):
                    st.markdown("#### Actual before/after comparison")
                    st.caption("Standard and fairness runs use the same workbook and hard rules. A negative Change means a lower measured value. A limited-time FEASIBLE solution is not guaranteed optimal.")
                    st.dataframe(result["comparison"], use_container_width=True, hide_index=True)
                elif result.get("comparison_requested"):
                    st.warning(result.get("comparison_unavailable", "Comparison unavailable; check baseline status."))
                    if result.get("baseline_log"):
                        with st.expander("Baseline CP-SAT log"):
                            st.code(result["baseline_log"][-12000:], language="text")
                else:
                    if result.get('rescheduled'):
                        st.info('This is the coordinator-approved revision. See Coordinator workflows → '
                                'Saved timetable versions to compare with the original generated schedule.')
                    else:
                        st.info("For a measured standard-vs-fairness comparison, enable the comparison checkbox in Workspace and generate again. It performs two solves.")
                from timetable_cloud_ai import explain_verified, get_api_key
                if st.session_state.get('agent_use_llm', False) and get_api_key():
                    if st.button('Explain measured fairness with cloud AI',
                                 key='cloud_explain_fairness'):
                        # A short aggregate-only excerpt: no names, student IDs or raw workbook.
                        verified = {k: q.get(k) for k in (
                            'teacher_gap_periods', 'section_avoidable_gap_proxy',
                            'teacher_daily_overload_periods', 'late_session_periods',
                            'preferred_free_slot_periods') if k in q}
                        summary = {'current_metrics': verified,
                                   'comparison': result.get('comparison') or [],
                                   'comparison_available': bool(result.get('comparison'))}
                        with st.spinner('Preparing an explanation of measured timetable fairness...'):
                            explanation, error = explain_verified(
                                'Explain the measured fairness results and trade-offs. '
                                'Do not claim one version is better without a complete comparison.',
                                json.dumps(summary)[:1900],
                                model=st.session_state.get('agent_model', 'openai/gpt-oss-120b'))
                        st.session_state['cloud_fairness_explanation'] = (explanation, error)
                    if st.session_state.get('cloud_fairness_explanation'):
                        explanation, error = st.session_state.cloud_fairness_explanation
                        if explanation:
                            st.markdown('**AI explanation of calculated fairness metrics**')
                            st.write(explanation)
                            st.caption('The numeric quality report above remains authoritative.')
                        else:
                            st.warning(error)
                st.caption('Individual timetable workload and gap tables are in Analytics dashboard. '
                           'Before/after faculty and section comparisons are shown above when both versions exist.')
                with st.expander("Measurement limitations"):
                    for line in quality.get("limitations", []):
                        st.write("• " + line)
                st.download_button("Download quality report (JSON)", files["quality_summary.json"],
                                   file_name="quality_summary.json", mime="application/json")

        with tab_policy:
            st.markdown("### Rules read from Institution_Policies")
            st.caption("These are the actual enabled policies interpreted from the uploaded Excel. "
                       "APPLIED means connected to the model, DATA_CHECKED means verified against explicit workbook mappings, "
                       "and CONDITIONAL means a soft objective depends on the solver mode. "
                       "This is not proof that unverified source records are official.")
            policy_file = files.get("policy_compliance.json")
            if policy_file:
                compliance = json.loads(policy_file.decode("utf-8", errors="replace"))
                summary = compliance.get("summary", {})
                pcols = st.columns(3)
                pcols[0].metric("Enabled policies", summary.get("enabled_rows", "—"))
                pcols[1].metric("Connected or data-checked", summary.get("connected_or_checked", "—"))
                pcols[2].metric("Unsupported or invalid", summary.get("unsupported_or_invalid", "—"))
                st.dataframe(compliance.get("policies", []), use_container_width=True, hide_index=True)
                if compliance.get("errors"):
                    st.error("Some enabled policies are unsupported or invalid. The timetable must not be treated as fully policy-compliant.")
                    for issue in compliance["errors"]: st.write("• " + issue)
                if compliance.get("warnings"):
                    with st.expander("Policy and dataset limitations"):
                        for issue in compliance["warnings"]: st.write("• " + issue)
                st.info("Open Elective mode: " + str(compliance.get("open_elective_window_mode", "unspecified")) +
                        " | Courses allowed inside the reservation: " +
                        (", ".join(compliance.get("allowed_window_course_codes", [])) or "none"))
                st.caption("Policy-defined soft-avoid slots: " + ", ".join(compliance.get("avoided_slot_ids", [])) +
                           " | Reserved elective slots: " + ", ".join(compliance.get("reserved_elective_slot_ids", [])))
                st.download_button("Download policy compliance report", files["policy_compliance.txt"],
                                   file_name="policy_compliance.txt", mime="text/plain")
            else:
                st.warning("No policy audit is available for this result. Run the updated solver.")

        with tab_workflows:
            from timetable_workflows_ui import show_workflows
            from timetable_versions import context_result
            if st.session_state.workbook_bytes:
                active = context_result(result, st.session_state.get('timetable_variant'))
                active_rows = list(csv.DictReader(io.StringIO(
                    active['files']['timetable_all.csv'].decode('utf-8-sig', errors='replace'))))
                show_workflows(st, active, active_rows, st.session_state.workbook_bytes)
            else:
                st.warning('The original Excel dataset is required for rescheduling.')

        with tab_history:
            st.markdown("### Saved timetable generations")
            st.caption("Manually saved timetables and protected coordinator-approved revisions appear here. Reopen a saved timetable as a working copy to request changes without replacing the archived original.")
            if not archive_versions:
                st.info("No saved versions are available yet. Generate a validated timetable first.")
            else:
                history_rows = []
                for version in archive_versions[:100]:
                    metrics_json = json.loads(version.get("metrics_json") or "{}")
                    history_rows.append({
                        "Version": version["version_id"],
                        "Variant": version.get("variant", "—"),
                        "Saved (UTC)": version.get("created_at", "—"),
                        "Faculty gaps": metrics_json.get("teacher_gap_periods", "—"),
                        "Late periods": metrics_json.get("late_session_periods", "—"),
                    })
                st.dataframe(history_rows, use_container_width=True, hide_index=True)
                selected_history = st.selectbox(
                    "Choose a saved version to download",
                    [v["version_id"] for v in archive_versions],
                    format_func=lambda vid: next(
                        f"{v['variant']} · {v['created_at']} · {vid}" for v in archive_versions if v['version_id'] == vid
                    ),
                    key="saved_versions_select_v11_4",
                )
                from timetable_records import get_version, data_digest
                version_data = get_version(data_digest(st.session_state.workbook_bytes), selected_history)
                if version_data:
                    st.caption('A saved CSV is an immutable source snapshot. Opening it for editing creates a separate working copy; approval records a new version.')
                    download_col, open_col, unsave_col = st.columns(3)
                    with download_col:
                        st.download_button(
                            "Download saved CSV", version_data["csv_blob"],
                            file_name=f"saved_timetable_{selected_history}.csv",
                            mime="text/csv", use_container_width=True)
                    with open_col:
                        if st.button('Open as working copy',
                                     key=f"open_saved_workcopy_{selected_history}",
                                     use_container_width=True, type='primary'):
                            try:
                                from timetable_versions import open_saved_as_working_copy
                                with st.spinner('Validating the saved timetable and preparing editable working copy...'):
                                    working = open_saved_as_working_copy(
                                        st.session_state.workbook_bytes, result, selected_history)
                                st.session_state.solver_result = working
                                st.session_state.assistant_history = []
                                st.session_state.coordinator_preview = None
                                st.session_state.coordinator_last_diagnosis = ''
                                st.session_state['reset_timetable_variant_on_next_run'] = True
                                st.session_state.last_manual_save_message = (
                                    f'Opened saved version {selected_history} as a working copy. '
                                    'The saved source has not been changed.')
                                st.rerun()
                            except Exception as exc:
                                st.error('Could not open saved version: ' + str(exc))
                    if st.button('Prepare saved version PDFs (Class / Faculty / Room)',
                                 key=f'prepare_saved_pdfs_{selected_history}', use_container_width=True):
                        try:
                            from timetable_persistence import saved_pdf_exports, read_workbook
                            saved_workbook = read_workbook(data_digest(st.session_state.workbook_bytes))
                            with st.spinner('Preparing independently validated archived PDFs...'):
                                from timetable_persistence import ensure_version_files
                                ensure_version_files(saved_workbook, selected_history)
                                st.session_state['archive_pdf_bundle'] = (
                                    selected_history, saved_pdf_exports(saved_workbook, selected_history))
                        except Exception as exc:
                            st.error('Archived PDF generation failed: ' + str(exc))
                    prepared = st.session_state.get('archive_pdf_bundle')
                    if prepared and prepared[0] == selected_history:
                        for key, title in (
                                ('all_sections.pdf', 'Class-wise (PDF)'),
                                ('all_faculty.pdf', 'Faculty-wise (PDF)'),
                                ('all_rooms.pdf', 'Room-wise (PDF)')):
                            st.download_button('Download saved ' + title, prepared[1][key],
                                file_name=f'{selected_history}_{key}', mime='application/pdf',
                                key=f'archived_pdf_{selected_history}_{key}')
                        archive_package = io.BytesIO()
                        with ZipFile(archive_package, 'w', ZIP_DEFLATED) as zipout:
                            zipout.writestr('source_sessions.csv', version_data['csv_blob'])
                            for item, export_name in (
                                    ('all_sections.pdf', 'classwise.pdf'),
                                    ('all_faculty.pdf', 'facultywise.pdf'),
                                    ('all_rooms.pdf', 'roomwise.pdf')):
                                zipout.writestr(export_name, prepared[1][item])
                        st.download_button('Download all 3 saved views (ZIP)',
                            archive_package.getvalue(),
                            file_name=f'{selected_history}_three_timetables.zip',
                            mime='application/zip', key=f'archived_zip_{selected_history}',
                            use_container_width=True)
                    from timetable_records import unsave_manual_version
                    removable = version_data['variant'] in ('STANDARD', 'FAIRNESS')
                    with unsave_col:
                        if st.button('Unsave selected version',
                                     key=f"history_unsave_prepare_{selected_history}",
                                     disabled=not removable, use_container_width=True):
                            st.session_state['confirm_history_unsave'] = selected_history
                    if not removable:
                        st.caption('Approved revisions and audit snapshots are protected from unsaving.')
                    if removable and st.session_state.get('confirm_history_unsave') == selected_history:
                        st.warning('Confirm removal of this saved version from local history. Class-change audit records are protected.')
                        confirm_col, cancel_col = st.columns(2)
                        with confirm_col:
                            if st.button('Confirm remove from history',
                                         key=f"history_unsave_confirm_{selected_history}",
                                         type='primary', use_container_width=True):
                                try:
                                    unsave_manual_version(
                                        data_digest(st.session_state.workbook_bytes),
                                        selected_history,
                                        expected_schedule_hash=version_data['schedule_hash'])
                                    st.session_state.pop('confirm_history_unsave', None)
                                    st.session_state.last_manual_save_message = 'Version removed from local history.'
                                    st.rerun()
                                except ValueError as exc:
                                    st.error('Cannot unsave: ' + str(exc))
                        with cancel_col:
                            if st.button('Cancel removal',
                                         key=f"history_unsave_cancel_{selected_history}",
                                         use_container_width=True):
                                st.session_state.pop('confirm_history_unsave', None)
                                st.rerun()
                st.caption("Validated Standard and Fairness timetables are saved automatically on this computer. Coordinator-approved changes are archived as separate versions. Save files are not account-protected or cloud-backed.")

        with tab_exports:
            st.markdown('<div style="margin-top:19px"><span class="section-eyebrow">KEEP YOUR OUTPUTS</span>'
                        '<h2 class="section-title-large">Download your timetables.</h2>'
                        '<p class="section-subtitle">Standard and Fairness remain separate CSV outputs. Downloading files is different from saving a version in the local history.</p></div>',
                        unsafe_allow_html=True)
            st.markdown('### Complete university timetables (PDF)')
            st.caption('Download **all sections**, **all faculty**, or **all rooms** as printable '
                       'weekly timetable grids, not just a list of sessions. '
                       'Choose **Combined PDF** to get all three views in one file.')
            from timetable_versions import active_version as _pdf_active_version
            # Always follow the actual timetable chosen in Results (Standard,
            # Fairness or approved working copy), rather than a stale CSV.
            pdf_selected = _pdf_active_version(result, selected_variant)
            try:
                with st.spinner('Preparing complete PDF timetables...'):
                    pdf_exports = _cached_full_pdf_exports(
                        pdf_selected['csv'], st.session_state.workbook_bytes,
                        st.session_state.get('university_name', '') or result.get('university_name', ''),
                        st.session_state.get('workbook_name', '') or result.get('input_file', ''),
                        pdf_selected['label'],
                        tuple(result.get('highlighted_session_ids', ()) or ())
                            if pdf_selected['kind'] in ('APPROVED','WORKING_COPY') else ())
                pdf_cols = st.columns(2)
                pdf_specs = (
                    ('all_sections.pdf', 'Download all Section-wise timetables (PDF)'),
                    ('all_faculty.pdf', 'Download all Faculty-wise timetables (PDF)'),
                    ('all_rooms.pdf', 'Download all Room-wise timetables (PDF)'),
                    ('all_three_views.pdf', 'Download all three views in ONE PDF'),
                )
                for i, (filename, label) in enumerate(pdf_specs):
                    pdf_cols[i % 2].download_button(
                        label, pdf_exports[filename], file_name=filename,
                        mime='application/pdf', use_container_width=True,
                        key='full_pdf_' + filename)
            except (ImportError, ValueError, OSError, RuntimeError) as exc:
                st.warning('PDF timetables are unavailable for this run: ' + str(exc))
                st.caption('Install required packages with: py -m pip install -r requirements.txt')
            st.markdown('---')
            if st.download_button("Download all generated files (ZIP)", _result_zip(files),
                                  file_name="timora_workspace_outputs.zip", mime="application/zip",
                                  use_container_width=True, type="primary"):
                pass
            exports = [
                ("timetable_sections.html", "Complete HTML timetable", "university_timetables.html", "text/html"),
                ("timetable_all.csv", "All sessions (CSV)", "timetable_all.csv", "text/csv"),
                ("timetable_faculty.csv", "Faculty-wise CSV", "timetable_faculty.csv", "text/csv"),
                ("timetable_rooms.csv", "Room-wise CSV", "timetable_rooms.csv", "text/csv"),
                ("timetable_groups.csv", "Group-wise CSV", "timetable_groups.csv", "text/csv"),
                ("validation_report.txt", "Validation report", "validation_report.txt", "text/plain"),
                ("independent_validation.txt", "Independent validation report", "independent_validation.txt", "text/plain"),
                ("independent_validation.json", "Independent validation JSON", "independent_validation.json", "application/json"),
                ("policy_compliance.txt", "Institution policy audit", "policy_compliance.txt", "text/plain"),
            ]
            for left in range(0, len(exports), 2):
                cols = st.columns(2)
                for col, (name, label, destination, mime) in zip(cols, exports[left:left+2]):
                    if name in files:
                        col.download_button(label, files[name], file_name=destination, mime=mime,
                                            use_container_width=True, key=f"download_{name}")
            st.markdown('<div class="notice-card">Timetable cards no longer repeat provisional-assignment badges. The CSV still contains the original <code>provisional</code> column, and the validation report retains the source-data warnings.</div>',
                        unsafe_allow_html=True)

        with tab_validation:
            st.markdown('<div style="margin-top:19px"><span class="section-eyebrow">TRANSPARENCY & VALIDATION</span>'
                        '<h2 class="section-title-large">Review the scheduling checks.</h2>'
                        '<p class="section-subtitle">An independent post-solve checker compares the generated CSV against the source Excel, including all required sessions. Individual student IDs are unavailable, so some elective conflicts remain unverified.</p></div>',
                        unsafe_allow_html=True)
            independent_bytes = files.get("independent_validation.json")
            if independent_bytes:
                ind = json.loads(independent_bytes.decode("utf-8"))
                vm = ind.get("metrics", {})
                vcols = st.columns(4)
                vcols[0].metric("Required sessions", vm.get("expected_sessions", "—"))
                vcols[1].metric("Scheduled sessions", vm.get("scheduled_sessions", "—"))
                vcols[2].metric("Hard violations", vm.get("hard_violations", "—"))
                vcols[3].metric("Soft-avoid occupied", vm.get("policy_avoided_occupied_rows", "—"))
                if ind.get("errors"):
                    st.error("Independent validator found hard violations. Inspect the detailed report.")
                else:
                    st.success("No hard violations detected in the implemented independent checks.")
                    st.info("Limit: synthetic room/faculty records and missing student IDs are not independently verified as real-world facts.")
                with st.expander("Independent validator details", expanded=True):
                    st.code(files.get("independent_validation.txt", b"").decode("utf-8"), language="text")
            else:
                st.warning("Independent validation output is unavailable for this run.")
            report_txt = files.get("validation_report.txt", b"").decode("utf-8", errors="replace")
            with st.expander("Validation report", expanded=True):
                st.code(report_txt or "No validation report is available.", language="text")
            with st.expander("CP-SAT solver log"):
                st.code(result.get("logs", "")[-20000:] or "No solver log available.", language="text")
    with st.container(key="floating_assistant_launcher", border=False):
        open_chat = st.button("Open Timora AI Assistant", key="open_floating_chat_v7",
                              help="Ask questions about your generated timetable", type="primary")
    if open_chat:
        show_coordinator_chat()

    if st.button("Back to Workspace", key="results_workspace"):
        go("studio")



top_navigation()
# A Streamlit rerun retains the browser's previous scroll position by default.
# Only on navigation/generation, request top-of-page after the new view mounts.
if st.session_state.pop('scroll_to_top_once', False):
    st.markdown('<span id="timora-page-start"></span>', unsafe_allow_html=True)
    st.components.v1.html(r"""<script>
      function resetPagePosition() {
        try {
          const p = window.parent;
          for (const el of [p.document.scrollingElement,
              p.document.querySelector('[data-testid="stAppViewContainer"]'),
              p.document.querySelector('[data-testid="stMain"]'),
              p.document.querySelector('section.main')]) {
            if (el) el.scrollTop = 0;
          }
          p.scrollTo({top:0,left:0,behavior:'instant'});
        } catch (_ignored) {
          try { window.parent.location.hash = 'timora-page-start'; } catch (_alsoIgnored) {}
        }
      }
      resetPagePosition();
      setTimeout(resetPagePosition, 90);
      setTimeout(resetPagePosition, 320);
    </script>""", height=0)
page = st.session_state.studio_page
if page == "home":
    home_page()
elif page == "studio":
    workspace_page()
elif page == "studio_quality":
    workspace_quality_page()
elif page == "results":
    results_page()
elif page == "assistant":
    st.session_state.studio_page = "results"
    results_page()
else:
    st.session_state.studio_page = "home"
    home_page()

st.markdown('<div class="site-footer"><span>Timora Workspace · Academic timetable planning</span>'
            '<span>Powered by Google OR-Tools CP-SAT · Local prototype scheduling studio</span></div>',
            unsafe_allow_html=True)
