import datetime
import json
import os
import socket
import threading
import time
import urllib.parse
from collections import deque
from flask import Flask, jsonify, render_template_string, request
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

API_TOKEN = os.environ.get(
    "COC_API_TOKEN",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiIsImtpZCI6IjI4YTMxOGY3LTAwMDAtYTFlYi03ZmExLTJjNzQzM2M2Y2NhNSJ9.eyJpc3MiOiJzdXBlcmNlbGwiLCJhdWQiOiJzdXBlcmNlbGw6Z2FtZWFwaSIsImp0aSI6IjcxMzBkZjBhLTNlYWQtNGQyZi05NThkLTcxMzJkNWNjYTVkYyIsImlhdCI6MTc5MjUwNjkwOSwic3ViIjoiZGV2ZWxvcGVyL2JkMDQ1MjVkLTljMDItNGJmNy1iM2JhLTc1NTI3YWQwNDk5MSIsInNjb3BlcyI6WyJjbGFzaCJdLCJsaW1pdHMiOlt7InRpZXIiOiJkZXZlbG9wZXIvc2lsdmVyIiwidHlwZSI6InRocm90dGxpbmcifSx7ImNpZHJzIjpbIjQ1Ljc5LjIxOC43OSJdLCJ0eXBlIjoiY2xpZW50In1dfQ.E5EMziuzZqcG_maxsWk9CgoobLLSIbwxWedZh8ENy95b0Z1SsbS4Ofw1eY4H6XVtW89zoMEMe-wmyv7dm5yWrw"
)
BASE_URL = os.environ.get("COC_BASE_URL", "https://cocproxy.royaleapi.dev/v1")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_SECRET = os.environ.get("ADMIN_PASSWORD", "admin123")

tracking_lock = threading.Lock()
clan_sliding_history = {}    
player_sliding_history = {}  
player_baselines = {}        

def get_game_day():
    # CoC "Daily" reset at 5:00 AM UTC (10:30 AM IST)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return (now_utc - datetime.timedelta(hours=5)).date()

current_day = get_game_day()

# Memory stores for instant live updates
live_clan_stats = {} 
live_players_cache = {} # Caches players across all clans for the Top 200 list

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set. Please add it to your Render Environment Variables.")
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        print("WARNING: No DATABASE_URL found. Skipping DB initialization.")
        return
        
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clans (
                    clan_tag TEXT PRIMARY KEY,
                    clan_name TEXT,
                    is_active INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clan_history (
                    id SERIAL PRIMARY KEY,
                    clan_tag TEXT,
                    record_date TEXT,
                    month_key TEXT,
                    season_donations INTEGER,
                    season_received INTEGER,
                    latest_daily INTEGER,
                    hourly_rate INTEGER,
                    member_count INTEGER,
                    timestamp TEXT,
                    epoch INTEGER
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clan_date ON clan_history(clan_tag, record_date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clan_month ON clan_history(clan_tag, month_key)")
        conn.commit()

def init_live_stats():
    if not DATABASE_URL: return
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.clan_tag, c.clan_name, 
                           COALESCE(h.season_donations, 0) as season_donations, 
                           COALESCE(h.latest_daily, 0) as latest_daily
                    FROM clans c
                    LEFT JOIN clan_history h ON c.clan_tag = h.clan_tag
                    WHERE h.id = (
                        SELECT MAX(id) FROM clan_history 
                        WHERE clan_tag = c.clan_tag
                    ) OR h.id IS NULL
                """)
                for row in cur.fetchall():
                    live_clan_stats[row["clan_tag"]] = {
                        "name": row["clan_name"] or "Unknown Clan",
                        "season_donations": row["season_donations"],
                        "latest_daily": row["latest_daily"]
                    }
    except Exception as e:
        print(f"Error loading live stats: {e}")

init_db()
init_live_stats()

def clean_old_records():
    if not DATABASE_URL: return
    cutoff = int(time.time()) - (62 * 86400)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM clan_history WHERE epoch < %s", (cutoff,))
        conn.commit()

def get_month_keys():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    this_month = now.strftime("%Y-%m")
    first_of_this_month = now.replace(day=1)
    prev_month = (first_of_this_month - datetime.timedelta(days=1)).strftime("%Y-%m")
    return this_month, prev_month

def get_xp_needed_for_next_level(exp_level):
    if exp_level <= 1:
        return 50
    elif exp_level <= 200:
        return (exp_level - 1) * 50
    elif exp_level < 300:
        return 10000 + (exp_level - 201) * 500
    else:
        return 60000 + (exp_level - 300) * 1000

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes" />
  <title>COCTRACKER // 2-MONTH TELEMETRY</title>
  <style>
    :root {
      --bg-canvas: #0a0e14;
      --card-bg: #121721;
      --card-border: #212b3d;
      --card-inner-bg: #141a26;
      --neon-cyan: #00e0fa;
      --neon-emerald: #10d985;
      --neon-amber: #ffb81e;
      --neon-purple: #a773fa;
      --neon-blue: #388bfd;
      --text-white: #f2f7fc;
      --text-muted: #73849e;
      --text-faint: #475569;
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    body {
      background-color: var(--bg-canvas);
      color: var(--text-white);
      display: flex;
      justify-content: center;
      min-height: 100vh;
      padding: 10px;
    }
    .app-window {
      width: 100%;
      max-width: 480px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-bottom: 24px;
    }
    .view-container {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .hidden {
      display: none !important;
    }
    .card {
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
    }

    /* Header */
    .header-card {
      min-height: 54px;
      flex-direction: row;
      justify-content: space-between;
      align-items: center;
    }
    .clan-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--neon-cyan);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      letter-spacing: 0.5px;
    }
    .sub-info {
      font-size: 10px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-top: 2px;
    }
    .badge-status {
      font-size: 11px;
      font-weight: 700;
      text-align: right;
      color: var(--text-muted);
      min-width: 80px;
    }
    .btn-menu {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 20px;
      font-weight: bold;
      cursor: pointer;
      padding: 0 4px;
      margin-left: 6px;
      transition: color 0.2s;
    }
    .btn-menu:hover { color: var(--neon-cyan); }

    /* Leaderboard Styles */
    .leaderboard-header {
      font-size: 14px;
      font-weight: 800;
      color: var(--neon-amber);
      margin-top: 5px;
      text-align: center;
      letter-spacing: 1px;
    }
    .lb-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-bottom: 5px;
    }
    .lb-item {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 10px;
      display: flex;
      align-items: center;
      gap: 12px;
      cursor: pointer;
      position: relative;
      transition: transform 0.1s ease, border-color 0.2s ease;
      user-select: none;
      -webkit-user-select: none;
      -webkit-touch-callout: none;
    }
    .lb-item:hover { border-color: var(--neon-cyan); }
    .lb-item:active { transform: scale(0.98); }
    
    .lb-rank {
      width: 30px;
      height: 30px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 14px;
      flex-shrink: 0;
      color: #fff;
      background: #212b3d;
    }
    .lb-rank.rank-1 { background: linear-gradient(135deg, #ffb81e, #f59e0b); color: #000; box-shadow: 0 0 10px rgba(255, 184, 30, 0.4); }
    .lb-rank.rank-2 { background: linear-gradient(135deg, #cbd5e1, #94a3b8); color: #000; }
    .lb-rank.rank-3 { background: linear-gradient(135deg, #f97316, #ea580c); color: #000; }
    .lb-details { flex: 1; display: flex; flex-direction: column; }
    .lb-name { font-size: 13px; font-weight: 700; color: var(--text-white); }
    .lb-tag { font-size: 10px; color: var(--text-muted); }
    .lb-stats { text-align: right; display: flex; flex-direction: column; }
    .lb-total { font-size: 13px; font-weight: 700; color: var(--neon-amber); }
    .lb-daily { font-size: 11px; font-weight: 700; color: var(--neon-emerald); }

    /* Delete Overlay */
    .del-overlay {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(18, 23, 33, 0.95);
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10;
      animation: fadeIn 0.2s ease-in-out;
    }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    .btn-del-confirm {
      background: #ef4444;
      color: white;
      border: none;
      padding: 8px 24px;
      border-radius: 6px;
      font-weight: 800;
      font-size: 12px;
      letter-spacing: 0.5px;
      cursor: pointer;
    }

    /* Inputs and Buttons */
    .input-card {
      gap: 8px;
      padding: 10px;
    }
    input[type="text"], input[type="password"] {
      width: 100%;
      height: 38px;
      background-color: #0d1421;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      color: var(--text-white);
      padding: 0 12px;
      font-size: 13px;
      outline: none;
    }
    input[type="text"]:focus, input[type="password"]:focus {
      border-color: var(--neon-cyan);
    }
    .btn-apply {
      height: 38px;
      background-color: #0073b3;
      border: none;
      border-radius: 6px;
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      width: 100%;
    }
    .btn-apply:active { background-color: #005a8c; }

    .btn-back-nav {
      height: 38px;
      background-color: #121a2f;
      border: 1px solid #1c283c;
      border-radius: 6px;
      color: var(--neon-cyan);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.5px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: background-color 0.2s;
      width: 100%;
    }
    .btn-back-nav:hover { background-color: #1a2542; }

    /* Month Selector Tabs */
    .month-tabs { display: flex; background: #0d131f; border: 1px solid var(--card-border); border-radius: 8px; padding: 3px; gap: 4px; }
    .tab-btn { flex: 1; height: 30px; background: transparent; border: none; border-radius: 6px; color: var(--text-muted); font-size: 11px; font-weight: 700; cursor: pointer; transition: all 0.2s; }
    .tab-btn.active { background: #172233; color: var(--neon-cyan); box-shadow: 0 1px 3px rgba(0,0,0,0.4); }

    /* Reset Timer Card & Metrics... */
    .reset-card { background-color: #0a121e; border: 1px solid rgba(0, 224, 250, 0.35); border-radius: 10px; padding: 10px 14px; gap: 4px; }
    .reset-header { display: flex; align-items: center; gap: 10px; }
    .icon-badge { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 700; flex-shrink: 0; }
    .badge-amber { background: rgba(255, 184, 30, 0.15); color: var(--neon-amber); border: 1px solid rgba(255, 184, 30, 0.4); }
    .badge-cyan { background: rgba(0, 224, 250, 0.15); color: var(--neon-cyan); border: 1px solid rgba(0, 224, 250, 0.4); }
    .badge-emerald { background: rgba(16, 217, 133, 0.15); color: var(--neon-emerald); border: 1px solid rgba(16, 217, 133, 0.4); }
    .badge-purple { background: rgba(167, 115, 250, 0.15); color: var(--neon-purple); border: 1px solid rgba(167, 115, 250, 0.4); }
    .reset-title { font-size: 11px; font-weight: 700; color: var(--neon-amber); letter-spacing: 0.5px; }
    .reset-sub { font-size: 9px; color: var(--text-muted); }
    .timer-tag { font-size: 10px; font-weight: 700; color: var(--text-muted); text-align: center; margin-top: 4px; letter-spacing: 0.5px; }
    .timer-countdown { font-size: 28px; font-weight: 700; text-align: center; color: var(--text-white); letter-spacing: 1px; }

    .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .metric-card { background: #0d131f; border: 1px solid var(--card-border); border-radius: 10px; padding: 8px 10px; display: flex; flex-direction: column; gap: 4px; }
    .metric-top { display: flex; align-items: center; gap: 6px; }
    .metric-title { font-size: 8.5px; font-weight: 700; color: var(--text-muted); letter-spacing: 0.5px; }
    .metric-val { font-size: 17px; font-weight: 700; }

    /* Historical Logs & Member List */
    .history-card { background-color: var(--card-inner-bg); border: 1px solid var(--card-border); border-radius: 10px; padding: 10px; gap: 6px; }
    .history-title { font-size: 11px; font-weight: 700; color: var(--neon-cyan); display: flex; justify-content: space-between; }
    .history-list { max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; margin-top: 4px; }
    .history-row { display: flex; justify-content: space-between; font-size: 10.5px; padding: 4px 6px; background: #0a0e14; border-radius: 4px; color: var(--text-muted); }
    .history-row .h-val { color: var(--neon-emerald); font-weight: 700; }
    .history-row .h-rate { color: var(--neon-purple); font-weight: 700; }

    .section-header { display: flex; justify-content: space-between; align-items: center; padding: 4px 4px 0 4px; }
    .sec-title { font-size: 10.5px; font-weight: 700; color: var(--neon-cyan); letter-spacing: 0.5px; }
    .sec-sub { font-size: 10px; color: var(--text-muted); }

    .members-list { display: flex; flex-direction: column; gap: 8px; }
    .member-card { background-color: var(--card-inner-bg); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; cursor: pointer; transition: border-color 0.15s ease, transform 0.1s ease; }
    .member-card:hover { border-color: rgba(0, 224, 250, 0.5); }
    .member-card:active { transform: scale(0.99); }

    .member-top-bar { display: flex; align-items: center; gap: 8px; }
    .rank-def { background-color: #212b3d; color: var(--text-white); }
    .exp-badge { padding: 3px 6px; background: rgba(16, 42, 85, 0.8); border: 1px solid rgba(56, 139, 253, 0.7); border-radius: 6px; font-size: 10.5px; font-weight: 700; color: #64b5f6; flex-shrink: 0; }
    .member-info-col { display: flex; flex-direction: column; overflow: hidden; flex: 1; }
    .player-name { font-size: 13.5px; font-weight: 700; color: var(--text-white); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .player-tag { font-size: 9.5px; color: var(--text-muted); }
    .role-badge { padding: 3px 8px; background: rgba(46, 35, 15, 0.8); border: 1px solid rgba(255, 184, 30, 0.6); border-radius: 6px; font-size: 8.5px; font-weight: 700; color: var(--neon-amber); white-space: nowrap; flex-shrink: 0; }
    .member-stats-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; padding-top: 4px; border-top: 1px solid rgba(255, 255, 255, 0.05); }
    .stats-col { display: flex; flex-direction: column; gap: 1px; }
    .stats-col-title { font-size: 9px; font-weight: 700; color: var(--text-muted); letter-spacing: 0.5px; margin-bottom: 2px; }
    .stat-line { font-size: 9.5px; display: flex; gap: 3px; align-items: baseline; }
    .stat-lbl { font-size: 8px; color: var(--text-muted); font-weight: 600; }

    /* Footer & Modals */
    .footer-card { min-height: 30px; background-color: #06090e; border-color: #17202e; flex-direction: row; justify-content: space-between; align-items: center; padding: 4px 10px; }
    .footer-left { font-size: 10px; color: var(--text-muted); }
    .footer-right { font-size: 10px; font-weight: 700; color: var(--neon-cyan); }

    .modal-backdrop { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background-color: rgba(0, 0, 0, 0.75); display: flex; align-items: center; justify-content: center; padding: 20px; z-index: 1000; opacity: 0; pointer-events: none; transition: opacity 0.2s ease; }
    .modal-backdrop.show { opacity: 1; pointer-events: auto; }
    .modal-card { width: 100%; max-width: 360px; background: #0f1624; border: 1px solid var(--neon-cyan); border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 10px; text-align: center; box-shadow: 0 10px 25px rgba(0,0,0,0.6); }
    .modal-title { font-size: 18px; font-weight: 700; color: var(--neon-cyan); }
    .modal-tag { font-size: 11px; color: var(--text-muted); margin-top: -6px; }
    .modal-body { font-size: 13px; line-height: 1.6; color: var(--text-white); padding: 10px 0; }
    .btn-dismiss { height: 38px; background-color: #1c283c; color: var(--text-white); border: none; border-radius: 6px; font-weight: 700; font-size: 12px; cursor: pointer; }
    .btn-dismiss:active { background-color: #263650; }
  </style>
</head>
<body>
  <div class="app-window">
    <!-- Top Header (Always Visible) -->
    <div class="card header-card">
      <div>
        <div class="clan-title" id="clanTitle">COCTRACKER</div>
        <div class="sub-info" id="subInfo">By LengAn</div>
      </div>
      <div style="display: flex; align-items: center;">
        <div class="badge-status" id="lblStatus">[ STANDBY ]</div>
        <button class="btn-menu" id="btnAdminMenu">⋮</button>
      </div>
    </div>

    <!-- VIEW 1: LEADERBOARD FRONT PAGE -->
    <div id="viewLeaderboard" class="view-container">
      <!-- Clan Leaderboard Section -->
      <div class="leaderboard-header">🏆 CLAN RANKINGS</div>
      
      <!-- Action Bar (Search, Top Players & Add Clan) -->
      <div style="display: flex; flex-direction: column; gap: 8px; margin: 4px 0 8px 0;">
        <input type="text" id="searchClan" placeholder="Search by Clan Name or Tag..." autocapitalize="none" />
        
        <!-- Toggle Button for Top Players -->
        <button class="btn-apply" id="btnShowTopPlayers" style="background-color: #6d28d9;">
          <span>⭐</span> TOP 200 PLAYERS (BY LEVEL)
        </button>

        <!-- Toggle Button for Add Clan -->
        <button class="btn-apply" id="btnShowAddForm">
          <span>+</span> ADD NEW CLAN
        </button>

        <!-- Hidden Add Clan Form -->
        <div class="card input-card hidden" id="addClanForm">
          <input type="text" id="inputClan" placeholder="Enter Clan Tag (e.g. #2Y9...)" autocapitalize="characters" />
          <div style="display: flex; gap: 8px;">
            <button class="btn-apply" id="btnApply" style="flex: 1;">ADD CLAN</button>
            <button class="btn-back-nav" id="btnCancelAdd" style="flex: 1;">CANCEL</button>
          </div>
        </div>
      </div>

      <div class="lb-list" id="leaderboardList">
        <div style="font-size:11px; color:var(--text-muted); text-align:center;">Loading rankings...</div>
      </div>
    </div>

    <!-- VIEW 2: DETAILED CLAN TELEMETRY -->
    <div id="viewClanDetail" class="view-container hidden">
      <!-- Back Button -->
      <button class="btn-back-nav" id="btnBack">← BACK TO RANKINGS</button>

      <!-- Month Switcher Tabs -->
      <div class="month-tabs">
        <button class="tab-btn active" id="tabCurrentMonth" onclick="selectMonthTab('current')">This Month</button>
        <button class="tab-btn" id="tabPrevMonth" onclick="selectMonthTab('previous')">Previous Month</button>
        <button class="tab-btn" id="tab7Day" onclick="selectMonthTab('7day')">Last 7 Days</button>
      </div>

      <!-- Daily Reset Countdown -->
      <div class="card reset-card">
        <div class="reset-header">
          <div class="icon-badge badge-amber">◷</div>
          <div>
            <div class="reset-title">DAILY DONATION RESETS</div>
            <div class="reset-sub">Counters reset every day at 10:30 AM IST</div>
          </div>
        </div>
        <div class="timer-tag">TIME UNTIL RESET</div>
        <div class="timer-countdown" id="lblCountdown">00:00:00</div>
      </div>

      <!-- 2x2 Telemetry Grid -->
      <div class="metrics-grid">
        <div class="metric-card">
          <div class="metric-top">
            <div class="icon-badge badge-amber" style="width:22px; height:22px; font-size:11px;">🏆</div>
            <span class="metric-title">SEASON DONATIONS</span>
          </div>
          <span class="metric-val" style="color:var(--neon-amber)" id="valSeasonDon">0</span>
        </div>

        <div class="metric-card">
          <div class="metric-top">
            <div class="icon-badge badge-cyan" style="width:22px; height:22px; font-size:11px;">↓</div>
            <span class="metric-title">DONATIONS RECEIVED</span>
          </div>
          <span class="metric-val" style="color:var(--neon-cyan)" id="valSeasonRec">0</span>
        </div>

        <div class="metric-card">
          <div class="metric-top">
            <div class="icon-badge badge-emerald" style="width:22px; height:22px; font-size:11px;">⚡</div>
            <span class="metric-title">LATEST DAILY</span>
          </div>
          <span class="metric-val" style="color:var(--neon-emerald)" id="valLatestDaily">+0</span>
        </div>

        <div class="metric-card">
          <div class="metric-top">
            <div class="icon-badge badge-purple" style="width:22px; height:22px; font-size:11px;">◷</div>
            <span class="metric-title">CLAN HOURLY DONATION</span>
          </div>
          <span class="metric-val" style="color:var(--neon-purple)" id="valClanHourly">0</span>
        </div>
      </div>

      <!-- 2-Month Saved Daily & Hourly History Log -->
      <div class="card history-card" id="historyCard">
        <div class="history-title">
          <span id="lblHistoryTitle">DAILY & HOURLY ARCHIVE</span>
          <span id="lblArchiveMonth" style="color:var(--text-muted); font-size:9.5px;"></span>
        </div>
        <div class="history-list" id="historyList">
          <div style="font-size:10px; color:var(--text-muted); text-align:center; padding:8px;">No historical logs yet. Records update hourly & daily.</div>
        </div>
      </div>

      <!-- Members Section Header -->
      <div class="section-header" style="flex-wrap: wrap; gap: 8px;">
        <span class="sec-title">CLAN MEMBERS | DONATION ROSTER</span>
        <div style="display: flex; align-items: center; gap: 8px;">
          <select id="sortMembers" style="background: #172233; color: var(--neon-cyan); border: 1px solid var(--card-border); border-radius: 4px; font-size: 10px; padding: 2px 4px; outline: none; cursor: pointer;">
            <option value="donations">Sort: Donations</option>
            <option value="level">Sort: Level</option>
          </select>
          <span class="sec-sub" id="lblMemberSummary">0/50 Members</span>
        </div>
      </div>

      <!-- Members Scroll Container -->
      <div class="members-list" id="membersList"></div>

      <!-- Telemetry Footer -->
      <div class="card footer-card">
        <span class="footer-left" id="lblFooterLeft">◷ Updated: Standby</span>
        <span class="footer-right" id="lblFooterRight">Next Poll: 60s</span>
      </div>
    </div>
    
    <!-- VIEW 3: GLOBAL TOP PLAYERS -->
    <div id="viewTopPlayers" class="view-container hidden">
      <button class="btn-back-nav" id="btnBackFromTop">← BACK TO RANKINGS</button>
      <div class="leaderboard-header" style="color: var(--neon-purple);">⭐ TOP 200 PLAYERS</div>
      <div class="members-list" id="topPlayersList" style="margin-top: 8px;">
          <div style="font-size:11px; color:var(--text-muted); text-align:center;">Loading players...</div>
      </div>
    </div>
  </div>

  <!-- Player Telemetry Modal -->
  <div class="modal-backdrop" id="playerModal">
    <div class="modal-card">
      <div class="modal-title" id="mPlayerName">PLAYER</div>
      <div class="modal-tag" id="mPlayerTag">TAG: #000000</div>
      <div class="modal-body" id="mModalBody">Querying IoT node telemetry...</div>
      <button class="btn-dismiss" id="btnDismissModal">DISMISS</button>
    </div>
  </div>
  
  <!-- Admin Authentication Modal -->
  <div class="modal-backdrop" id="adminModal">
    <div class="modal-card">
      <div class="modal-title" id="adminModalTitle">ADMIN LOGIN</div>
      <div class="modal-body">
        <input type="password" id="adminPassword" placeholder="Enter Admin Password" style="margin-bottom:12px;" />
        <div style="display:flex; gap:8px;">
          <button class="btn-apply" id="btnLogin" style="flex:1;">LOGIN</button>
          <button class="btn-back-nav" id="btnLogout" style="flex:1; display:none;">LOGOUT</button>
          <button class="btn-dismiss" id="btnCloseAdminModal" style="flex:1;">CANCEL</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    const INTERVAL_SECONDS = 60;
    let clanId = "";
    let activeMonthTab = "current";
    let remainingTime = INTERVAL_SECONDS;
    let currentPlayers = [];
    let currentSort = "donations";
    
    // --- ADMIN LOGIC ---
    let adminKey = localStorage.getItem("coctracker_admin_key") || "";
    let isAdmin = !!adminKey;

    const adminModal = document.getElementById("adminModal");
    const btnAdminMenu = document.getElementById("btnAdminMenu");
    const btnCloseAdminModal = document.getElementById("btnCloseAdminModal");
    const btnLogin = document.getElementById("btnLogin");
    const btnLogout = document.getElementById("btnLogout");
    const adminPassword = document.getElementById("adminPassword");
    const adminModalTitle = document.getElementById("adminModalTitle");

    btnAdminMenu.onclick = () => {
        if (isAdmin) {
            adminPassword.style.display = "none";
            btnLogin.style.display = "none";
            btnLogout.style.display = "block";
            adminModalTitle.textContent = "ADMIN ACCESS (LOGGED IN)";
        } else {
            adminPassword.style.display = "block";
            btnLogin.style.display = "block";
            btnLogout.style.display = "none";
            adminPassword.value = "";
            adminModalTitle.textContent = "ADMIN LOGIN";
        }
        adminModal.classList.add("show");
    };

    btnCloseAdminModal.onclick = () => adminModal.classList.remove("show");

    btnLogin.onclick = () => {
        const pwd = adminPassword.value.trim();
        if(pwd) {
            adminKey = pwd;
            isAdmin = true;
            localStorage.setItem("coctracker_admin_key", adminKey);
            adminModal.classList.remove("show");
        }
    };

    btnLogout.onclick = () => {
        adminKey = "";
        isAdmin = false;
        localStorage.removeItem("coctracker_admin_key");
        adminModal.classList.remove("show");
    };
    // -------------------

    // View Containers
    const viewLeaderboard = document.getElementById("viewLeaderboard");
    const viewClanDetail = document.getElementById("viewClanDetail");
    const viewTopPlayers = document.getElementById("viewTopPlayers");

    const clanTitle = document.getElementById("clanTitle");
    const subInfo = document.getElementById("subInfo");
    const lblStatus = document.getElementById("lblStatus");
    
    // Form and Search Elements
    const searchClan = document.getElementById("searchClan");
    const btnShowAddForm = document.getElementById("btnShowAddForm");
    const btnShowTopPlayers = document.getElementById("btnShowTopPlayers");
    const addClanForm = document.getElementById("addClanForm");
    const btnCancelAdd = document.getElementById("btnCancelAdd");
    const inputClan = document.getElementById("inputClan");
    const btnApply = document.getElementById("btnApply");
    
    const btnBackFromTop = document.getElementById("btnBackFromTop");
    const topPlayersList = document.getElementById("topPlayersList");

    const lblCountdown = document.getElementById("lblCountdown");
    const lblMemberSummary = document.getElementById("lblMemberSummary");
    const membersList = document.getElementById("membersList");
    const historyList = document.getElementById("historyList");
    const lblArchiveMonth = document.getElementById("lblArchiveMonth");
    const lblHistoryTitle = document.getElementById("lblHistoryTitle");

    const valSeasonDon = document.getElementById("valSeasonDon");
    const valSeasonRec = document.getElementById("valSeasonRec");
    const valLatestDaily = document.getElementById("valLatestDaily");
    const valClanHourly = document.getElementById("valClanHourly");

    const lblFooterLeft = document.getElementById("lblFooterLeft");
    const lblFooterRight = document.getElementById("lblFooterRight");

    const playerModal = document.getElementById("playerModal");
    const mPlayerName = document.getElementById("mPlayerName");
    const mPlayerTag = document.getElementById("mPlayerTag");
    const mModalBody = document.getElementById("mModalBody");
    const btnDismissModal = document.getElementById("btnDismissModal");
    
    const sortMembersSelect = document.getElementById("sortMembers");

    btnDismissModal.onclick = () => playerModal.classList.remove("show");

    sortMembersSelect.addEventListener("change", (e) => {
        currentSort = e.target.value;
        applySortAndRender();
    });

    function applySortAndRender() {
        if (!currentPlayers || currentPlayers.length === 0) return;
        
        let sortedPlayers = [...currentPlayers];
        
        if (currentSort === "donations") {
            sortedPlayers.sort((a, b) => b.donations - a.donations);
        } else if (currentSort === "level") {
            sortedPlayers.sort((a, b) => b.expLevel - a.expLevel);
        }
        
        renderMembers(sortedPlayers);
    }

    // Initialize base history state on page load based on current URL hash
    if (window.location.hash.startsWith("#clan=")) {
        history.replaceState({ page: 'detail' }, '', window.location.href);
    } else if (window.location.hash === "#top") {
        history.replaceState({ page: 'top' }, '', window.location.href);
    } else {
        history.replaceState({ page: 'home' }, '', window.location.pathname);
    }

    // Add Clan UI Toggles
    btnShowAddForm.addEventListener("click", () => {
      btnShowAddForm.classList.add("hidden");
      addClanForm.classList.remove("hidden");
      inputClan.focus();
    });

    btnCancelAdd.addEventListener("click", () => {
      addClanForm.classList.add("hidden");
      btnShowAddForm.classList.remove("hidden");
      inputClan.value = "";
    });

    // UI Navigation logic and History states
    function showDetailView(c_tag) {
        clanId = c_tag;
        inputClan.value = "";
        
        addClanForm.classList.add("hidden");
        btnShowAddForm.classList.remove("hidden");

        viewLeaderboard.classList.add("hidden");
        viewTopPlayers.classList.add("hidden");
        viewClanDetail.classList.remove("hidden");
        
        const newHash = '#clan=' + encodeURIComponent(c_tag);
        if (window.location.hash !== newHash) {
            history.pushState({ page: 'detail' }, '', newHash);
        }
        
        fetchData();
    }

    // Top Players Viewer
    async function loadTopPlayers() {
        viewLeaderboard.classList.add("hidden");
        viewClanDetail.classList.add("hidden");
        viewTopPlayers.classList.remove("hidden");
        
        topPlayersList.innerHTML = `<div style="font-size:11px; color:var(--text-muted); text-align:center;">Loading top players...</div>`;
        
        try {
            const res = await fetch("/api/top_players");
            const data = await res.json();
            
            topPlayersList.innerHTML = "";
            if (!data.players || data.players.length === 0) {
                topPlayersList.innerHTML = `<div style="font-size:11px; color:var(--text-muted); text-align:center;">No player data available yet. Please wait for the first sync.</div>`;
                return;
            }
            
            data.players.forEach((p, idx) => {
                const rank = idx + 1;
                let rankClass = "rank-def";
                if (rank === 1) rankClass = "rank-1";
                else if (rank === 2) rankClass = "rank-2";
                else if (rank === 3) rankClass = "rank-3";

                const cleanRole = p.role.replace("ADMIN", "ELDER");
                const clanBadge = p.clan_name ? `<div style="font-size: 9.5px; color: var(--neon-purple); margin-top:2px; font-weight:bold;">🛡️ ${escapeHtml(p.clan_name)}</div>` : "";
                
                const card = document.createElement("div");
                card.className = "member-card";
                card.onclick = () => openPlayerModal(p);

                card.innerHTML = `
                  <div class="member-top-bar">
                    <div class="rank-box ${rankClass}" style="width:26px; height:26px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:bold; font-size:11px; flex-shrink:0;">${rank}</div>
                    <div class="exp-badge">★ ${p.expLevel}</div>
                    <div class="member-info-col">
                      <span class="player-name">${escapeHtml(p.name)}</span>
                      <span class="player-tag">${escapeHtml(p.tag)}</span>
                      ${clanBadge}
                    </div>
                    <div class="role-badge">👑 ${escapeHtml(cleanRole)}</div>
                  </div>
                  <div class="member-stats-grid" style="grid-template-columns: 1fr 1fr;">
                    <div class="stats-col">
                      <span class="stats-col-title">TODAY</span>
                      <div class="stat-line"><span style="color:var(--neon-emerald)">+${p.daily.toLocaleString()}</span> <span class="stat-lbl">DONATED</span></div>
                    </div>
                    <div class="stats-col">
                      <span class="stats-col-title">SEASON</span>
                      <div class="stat-line"><span style="color:var(--neon-amber)">+${p.donations.toLocaleString()}</span> <span class="stat-lbl">DONATED</span></div>
                    </div>
                  </div>
                `;
                topPlayersList.appendChild(card);
            });
        } catch (e) {
            topPlayersList.innerHTML = `<div style="font-size:11px; color:#ef4444; text-align:center;">Error loading players.</div>`;
        }
    }
    
    btnShowTopPlayers.addEventListener("click", () => {
        history.pushState({ page: 'top' }, '', '#top');
        loadTopPlayers();
    });

    // Catch hardware/browser Back Button
    window.addEventListener('popstate', (e) => {
        if (e.state && e.state.page === 'detail') {
            viewLeaderboard.classList.add("hidden");
            viewTopPlayers.classList.add("hidden");
            viewClanDetail.classList.remove("hidden");
        } else if (e.state && e.state.page === 'top') {
            viewLeaderboard.classList.add("hidden");
            viewClanDetail.classList.add("hidden");
            viewTopPlayers.classList.remove("hidden");
        } else {
            viewClanDetail.classList.add("hidden");
            viewTopPlayers.classList.add("hidden");
            viewLeaderboard.classList.remove("hidden");
            clanId = "";
            
            clanTitle.textContent = "COCTRACKER";
            subInfo.textContent = "By LengAn";
            subInfo.style.color = "var(--text-muted)";
            lblStatus.textContent = "[ STANDBY ]";
            lblStatus.style.color = "var(--text-muted)";
        }
    });

    document.getElementById("btnBack").onclick = () => { history.back(); };
    btnBackFromTop.onclick = () => { history.back(); };

    function selectMonthTab(tab) {
      activeMonthTab = tab;
      document.getElementById("tabCurrentMonth").className = `tab-btn ${tab === 'current' ? 'active' : ''}`;
      document.getElementById("tabPrevMonth").className = `tab-btn ${tab === 'previous' ? 'active' : ''}`;
      document.getElementById("tab7Day").className = `tab-btn ${tab === '7day' ? 'active' : ''}`;
      loadHistoricalLogs();
    }

    function updateDailyResetCountdown() {
      const now = new Date();
      const target = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 5, 0, 0, 0));
      if (now >= target) {
        target.setUTCDate(target.getUTCDate() + 1);
      }
      const diffSecs = Math.max(0, Math.floor((target - now) / 1000));
      const hours = Math.floor(diffSecs / 3600);
      const mins = Math.floor((diffSecs % 3600) / 60);
      const secs = diffSecs % 60;
      lblCountdown.textContent = `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }

    async function loadHistoricalLogs() {
      if (!clanId) return;
      try {
        let url = `/api/history?clan_tag=${encodeURIComponent(clanId)}&period=${activeMonthTab}`;
        if (activeMonthTab === '7day') {
            url = `/api/history_7day?clan_tag=${encodeURIComponent(clanId)}`;
        }
        
        const res = await fetch(url);
        const data = await res.json();
        
        lblHistoryTitle.textContent = activeMonthTab === '7day' ? 'DAILY ARCHIVE' : 'DAILY & HOURLY ARCHIVE';
        lblArchiveMonth.textContent = activeMonthTab === '7day' ? 'Period: Last 7 Days' : `Period: ${data.period_label}`;
        historyList.innerHTML = "";

        if (!data.records || data.records.length === 0) {
          historyList.innerHTML = `<div style="font-size:10px; color:var(--text-muted); text-align:center; padding:8px;">No records saved.</div>`;
          return;
        }

        data.records.forEach(r => {
          const row = document.createElement("div");
          row.className = "history-row";
          
          if (activeMonthTab === '7day') {
              // 7-Day View: Shows only the day and the total 24H daily donations
              row.innerHTML = `
                <span style="color:var(--text-white); font-weight:600;">📅 ${escapeHtml(r.record_date)}</span>
                <span>Total 24H: <span class="h-val" style="font-size:11.5px;">+${Number(r.max_daily).toLocaleString()}</span></span>
              `;
          } else {
              // Current/Previous Month View: Shows timestamp and hourly breakdown
              row.innerHTML = `
                <span>${escapeHtml(r.record_date)} <span style="font-size:9px; color:var(--text-faint)">${escapeHtml(r.time)}</span></span>
                <span>Daily: <span class="h-val">+${Number(r.latest_daily).toLocaleString()}</span></span>
                <span>Hourly: <span class="h-rate">+${Number(r.hourly_rate).toLocaleString()}/h</span></span>
              `;
          }
          historyList.appendChild(row);
        });
      } catch (e) {
        console.error("Failed loading logs", e);
      }
    }

    function showDeleteOption(element, clanTag) {
        if (element.querySelector('.del-overlay')) return;
        
        const overlay = document.createElement('div');
        overlay.className = 'del-overlay';
        overlay.innerHTML = `<button class="btn-del-confirm">DELETE CLAN</button>`;
        
        overlay.querySelector('.btn-del-confirm').onclick = async (ev) => {
            ev.stopPropagation();
            const res = await fetch(`/api/clans/delete?tag=${encodeURIComponent(clanTag)}`, { 
                method: "POST",
                headers: {
                    "X-Admin-Key": adminKey
                }
            });
            
            if (res.status === 401) {
                alert("Unauthorized: Invalid Admin Password.");
                btnLogout.click(); // force logout if token is bad
            } else {
                loadLeaderboard();
            }
        };
        
        element.appendChild(overlay);
        
        setTimeout(() => {
            if(overlay.parentNode) overlay.parentNode.removeChild(overlay);
        }, 4000);
    }

    async function loadLeaderboard() {
      try {
        const res = await fetch("/api/leaderboard");
        const data = await res.json();
        const lbList = document.getElementById("leaderboardList");
        lbList.innerHTML = "";

        if (!data.leaderboard || data.leaderboard.length === 0) {
          lbList.innerHTML = `<div style="font-size:11px; color:var(--text-muted); text-align:center;">No clans tracked yet. Add a clan above.</div>`;
          return;
        }

        data.leaderboard.forEach((clan, index) => {
          const rank = index + 1;
          let rankClass = "";
          if (rank === 1) rankClass = "rank-1";
          else if (rank === 2) rankClass = "rank-2";
          else if (rank === 3) rankClass = "rank-3";

          const item = document.createElement("div");
          item.className = "lb-item";
          
          item.innerHTML = `
            <div class="lb-rank ${rankClass}">${rank}</div>
            <div class="lb-details">
              <span class="lb-name">${escapeHtml(clan.clan_name || "Unknown Clan")}</span>
              <span class="lb-tag">${escapeHtml(clan.clan_tag)}</span>
            </div>
            <div class="lb-stats">
              <span class="lb-total">${Number(clan.season_donations).toLocaleString()}</span>
              <span class="lb-daily">+${Number(clan.latest_daily).toLocaleString()} today</span>
            </div>
          `;

          let pressTimer;
          let isLongPress = false;
          
          const startPress = (e) => {
              if (!isAdmin) return;
              isLongPress = false;
              pressTimer = setTimeout(() => {
                  isLongPress = true;
                  showDeleteOption(item, clan.clan_tag);
              }, 500); 
          };
          
          const endPress = () => {
              clearTimeout(pressTimer);
          };
          
          item.addEventListener('touchstart', startPress, {passive: true});
          item.addEventListener('touchend', endPress);
          item.addEventListener('touchcancel', endPress);
          item.addEventListener('mousedown', startPress);
          item.addEventListener('mouseup', endPress);
          item.addEventListener('mouseleave', endPress);
          
          item.addEventListener('contextmenu', (e) => {
              if (!isAdmin) return;
              e.preventDefault();
              isLongPress = true;
              showDeleteOption(item, clan.clan_tag);
          });
          
          item.onclick = (e) => {
              if (isLongPress) {
                  e.preventDefault();
                  return;
              }
              if (e.target.closest('.del-overlay')) return;
              
              showDetailView(clan.clan_tag);
          };

          lbList.appendChild(item);
        });

        const currentSearch = searchClan.value;
        if (currentSearch) {
          searchClan.dispatchEvent(new Event("input"));
        }

      } catch (e) {
        console.error("Failed to load leaderboard", e);
      }
    }

    async function fetchData() {
      if (!clanId) return;
      try {
        lblStatus.textContent = "[ SYNCING ]";
        lblStatus.style.color = "var(--neon-cyan)";

        const res = await fetch(`/api/track?clan_tag=${encodeURIComponent(clanId)}`);
        const data = await res.json();

        if (!res.ok) throw new Error(data.error || "Telemetry failed");

        clanTitle.textContent = data.clan_name.toUpperCase();
        subInfo.textContent = `TAG: ${data.clan_tag} | 24/7 ONLINE`;
        subInfo.style.color = "var(--text-muted)";

        lblStatus.textContent = "● 24/7 LIVE";
        lblStatus.style.color = "var(--neon-emerald)";

        lblMemberSummary.textContent = `${data.member_count}/50 Members`;

        valSeasonDon.textContent = Number(data.season_donations).toLocaleString();
        valSeasonRec.textContent = Number(data.season_received).toLocaleString();
        valLatestDaily.textContent = `+${Number(data.latest_daily).toLocaleString()}`;
        valClanHourly.textContent = Number(data.clan_hourly).toLocaleString();

        currentPlayers = data.players;
        applySortAndRender();

        lblFooterLeft.textContent = `◷ Updated: ${data.timestamp}`;
        remainingTime = INTERVAL_SECONDS;
        lblFooterRight.textContent = `Next Poll: ${remainingTime}s`;

        loadHistoricalLogs();
        loadLeaderboard(); 
      } catch (err) {
        lblStatus.textContent = "[ ERROR ]";
        lblStatus.style.color = "#ef4444";
        subInfo.textContent = `Telemetry Error: ${err.message.slice(0, 40)}`;
        subInfo.style.color = "#ef4444";
      }
    }

    function renderMembers(players) {
      membersList.innerHTML = "";

      players.forEach((p, idx) => {
        const rank = idx + 1;
        let rankClass = "rank-def";
        if (rank === 1) rankClass = "rank-1";
        else if (rank === 2) rankClass = "rank-2";
        else if (rank === 3) rankClass = "rank-3";

        const cleanRole = p.role.replace("ADMIN", "ELDER");
        const hourlyDonStr = p.hourlyDonated > 0 ? `+${p.hourlyDonated.toLocaleString()}` : "—";
        const hourlyRecStr = p.hourlyReceived > 0 ? `+${p.hourlyReceived.toLocaleString()}` : "—";

        const card = document.createElement("div");
        card.className = "member-card";
        card.onclick = () => openPlayerModal(p);

        card.innerHTML = `
          <div class="member-top-bar">
            <div class="rank-box ${rankClass}">${rank}</div>
            <div class="exp-badge">★ ${p.expLevel}</div>
            <div class="member-info-col">
              <span class="player-name">${escapeHtml(p.name)}</span>
              <span class="player-tag">${escapeHtml(p.tag)}</span>
            </div>
            <div class="role-badge">👑 ${escapeHtml(cleanRole)}</div>
          </div>
          <div class="member-stats-grid">
            <div class="stats-col">
              <span class="stats-col-title">TODAY</span>
              <div class="stat-line"><span style="color:var(--neon-emerald)">+${p.daily.toLocaleString()}</span> <span class="stat-lbl">DONATED</span></div>
              <div class="stat-line"><span style="color:var(--neon-cyan)">+${p.donationsReceived.toLocaleString()}</span> <span class="stat-lbl">RECEIVED</span></div>
            </div>
            <div class="stats-col">
              <span class="stats-col-title">SEASON</span>
              <div class="stat-line"><span style="color:var(--neon-amber)">+${p.donations.toLocaleString()}</span> <span class="stat-lbl">DONATED</span></div>
              <div class="stat-line"><span style="color:var(--neon-cyan)">+${p.donationsReceived.toLocaleString()}</span> <span class="stat-lbl">RECEIVED</span></div>
            </div>
            <div class="stats-col">
              <span class="stats-col-title">HOURLY</span>
              <div class="stat-line"><span style="color:var(--neon-emerald)">${hourlyDonStr}</span> <span class="stat-lbl">DONATED</span></div>
              <div class="stat-line"><span style="color:var(--neon-cyan)">${hourlyRecStr}</span> <span class="stat-lbl">RECEIVED</span></div>
            </div>
          </div>
        `;
        membersList.appendChild(card);
      });
    }

    async function openPlayerModal(player) {
      mPlayerName.textContent = player.name;
      mPlayerTag.textContent = `TAG: ${player.tag}`;
      mModalBody.innerHTML = `<i>Querying IoT node telemetry...</i>`;
      playerModal.classList.add("show");

      try {
        const res = await fetch(`/api/player?player_tag=${encodeURIComponent(player.tag)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed to load player");

        mModalBody.innerHTML = `
          <div style="margin-bottom:8px"><b>Town Hall:</b> ${data.townHallLevel} &nbsp;|&nbsp; <b>Role:</b> ${escapeHtml(data.role)}</div>
          <div><b>War Stars:</b> <span style="color:var(--neon-amber)">${Number(data.warStars).toLocaleString()}</span></div>
          <div><b>Exp Level:</b> <span style="color:var(--neon-cyan)">${data.expLevel}</span></div>
          <div><b>XP Needed for Next Level:</b> <span style="color:var(--neon-emerald)">${Number(data.xpNeeded).toLocaleString()}</span></div>
        `;
      } catch (err) {
        mModalBody.innerHTML = `<span style="color:#ef4444">Node offline or error:<br>${escapeHtml(err.message)}</span>`;
      }
    }

    function escapeHtml(str) {
      return String(str).replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[m]);
    }

    btnApply.addEventListener("click", () => {
      const clanVal = inputClan.value.trim();
      if (!clanVal) {
        lblStatus.textContent = "[ NO CLAN ]";
        lblStatus.style.color = "#ef4444";
        return;
      }
      showDetailView(clanVal);
    });

    searchClan.addEventListener("input", (e) => {
      const term = e.target.value.toLowerCase();
      const items = document.querySelectorAll(".lb-item");
      
      items.forEach(item => {
        const name = item.querySelector(".lb-name").textContent.toLowerCase();
        const tag = item.querySelector(".lb-tag").textContent.toLowerCase();
        
        if (name.includes(term) || tag.includes(term)) {
          item.style.display = "flex"; 
        } else {
          item.style.display = "none"; 
        }
      });
    });

    setInterval(() => {
      updateDailyResetCountdown();
      
      if (clanId && !viewClanDetail.classList.contains("hidden")) {
        remainingTime -= 1;
        if (remainingTime <= 0) {
          remainingTime = INTERVAL_SECONDS;
          fetchData();
        }
        lblFooterRight.textContent = `Next Poll: ${remainingTime}s`;
      }
      
      if ((!clanId || viewClanDetail.classList.contains("hidden")) && viewTopPlayers.classList.contains("hidden")) {
         if (new Date().getSeconds() % 20 === 0) {
            loadLeaderboard();
         }
      }
    }, 1000);

    // Initial load respects the URL hash if user refreshes
    updateDailyResetCountdown();
    if (window.location.hash.startsWith("#clan=")) {
        const hashClan = decodeURIComponent(window.location.hash.substring(6));
        showDetailView(hashClan);
    } else if (window.location.hash === "#top") {
        loadTopPlayers();
    } else {
        loadLeaderboard();
    }
  </script>
</body>
</html>
"""

def execute_clan_poll(clan_tag):
    global current_day, clan_sliding_history, player_sliding_history, player_baselines, live_clan_stats, live_players_cache

    with tracking_lock:
        if not clan_tag:
            return None, "No Clan Tag provided"

        clan_tag = clan_tag if clan_tag.startswith("#") else f"#{clan_tag}"

        url = f"{BASE_URL}/clans/{urllib.parse.quote(clan_tag)}"
        headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            raw_clan_data = resp.json()
        except requests.exceptions.HTTPError:
            return None, f"Supercell HTTP {resp.status_code} (Check IP or Tag)"
        except Exception as e:
            return None, str(e)

        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        game_day = get_game_day()
        current_epoch = int(now.timestamp())
        date_str = game_day.strftime("%Y-%m-%d")
        month_str = game_day.strftime("%Y-%m")

        if game_day != current_day:
            current_day = game_day
            player_baselines.clear()

        clan_name = raw_clan_data.get("name", "Unknown Clan")
        member_count = raw_clan_data.get("members", len(raw_clan_data.get("memberList", [])))
        member_list = raw_clan_data.get("memberList", [])

        season_donations_total = 0
        season_received_total = 0
        latest_daily_total = 0
        parsed_members = []

        if clan_tag not in clan_sliding_history:
            clan_sliding_history[clan_tag] = deque()

        for member in member_list:
            donations = member.get("donations", 0)
            received = member.get("donationsReceived", 0)
            season_donations_total += donations
            season_received_total += received

            tag = member.get("tag", "").strip()
            name = member.get("name", "").strip()
            p_key = f"{clan_tag}:{tag}"

            if p_key not in player_baselines or donations < player_baselines[p_key]:
                player_baselines[p_key] = donations

            daily = donations - player_baselines[p_key]
            latest_daily_total += daily

            if p_key not in player_sliding_history:
                player_sliding_history[p_key] = deque()
            p_history = player_sliding_history[p_key]
            p_history.append((current_epoch, donations, received))
            while p_history and (current_epoch - p_history[0][0]) > 3600:
                p_history.popleft()

            hourly_donated = max(0, donations - p_history[0][1]) if p_history else 0
            hourly_received = max(0, received - p_history[0][2]) if p_history else 0

            parsed_members.append({
                "name": name,
                "tag": tag,
                "role": member.get("role", "member").upper(),
                "donations": donations,
                "donationsReceived": received,
                "daily": daily,
                "hourlyDonated": hourly_donated,
                "hourlyReceived": hourly_received,
                "expLevel": member.get("expLevel", 0),
                "trophies": member.get("trophies", 0),
            })

        parsed_members.sort(key=lambda m: m["donations"], reverse=True)

        c_hist = clan_sliding_history[clan_tag]
        c_hist.append((current_epoch, season_donations_total))
        while c_hist and (current_epoch - c_hist[0][0]) > 3600:
            c_hist.popleft()

        clan_hourly = max(0, season_donations_total - c_hist[0][1])

        live_clan_stats[clan_tag] = {
            "name": clan_name,
            "season_donations": season_donations_total,
            "latest_daily": latest_daily_total
        }
        
        # Save parsed players in memory for the Top 200 route
        live_players_cache[clan_tag] = parsed_members

        # --- PERSIST TO POSTGRES DATABASE ---
        if DATABASE_URL:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO clans (clan_tag, clan_name, is_active)
                        VALUES (%s, %s, 1)
                        ON CONFLICT(clan_tag) DO UPDATE SET clan_name=EXCLUDED.clan_name, is_active=1
                    """, (clan_tag, clan_name))

                    cur.execute("UPDATE clans SET is_active=0 WHERE clan_tag != %s", (clan_tag,))

                    cur.execute("""
                        SELECT epoch, timestamp FROM clan_history 
                        WHERE clan_tag = %s ORDER BY id DESC LIMIT 1
                    """, (clan_tag,))
                    last_entry = cur.fetchone()

                    current_hour_label = now.strftime("%I:00:00 %p")

                    if not last_entry or last_entry[1] != current_hour_label:
                        cur.execute("""
                            INSERT INTO clan_history (
                                clan_tag, record_date, month_key, season_donations,
                                season_received, latest_daily, hourly_rate,
                                member_count, timestamp, epoch
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            clan_tag, date_str, month_str, season_donations_total,
                            season_received_total, latest_daily_total, clan_hourly,
                            member_count, current_hour_label, current_epoch
                        ))
                conn.commit()

        payload = {
            "clan_name": clan_name,
            "clan_tag": clan_tag,
            "member_count": member_count,
            "players": parsed_members,
            "season_donations": season_donations_total,
            "season_received": season_received_total,
            "latest_daily": latest_daily_total,
            "clan_hourly": clan_hourly,
            "timestamp": now.strftime("%I:%M:%S %p"),
        }

        return payload, None

def background_worker_loop():
    while True:
        try:
            if DATABASE_URL:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT clan_tag FROM clans")
                        clans = cur.fetchall()

                for (c_tag,) in clans:
                    execute_clan_poll(c_tag)
                    time.sleep(2)  

                clean_old_records()

            self_url = os.environ.get("RENDER_EXTERNAL_URL")
            if self_url:
                requests.get(f"{self_url}/api/ping", timeout=5)
        except Exception:
            pass
        time.sleep(60)

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/api/ping")
def ping():
    return jsonify({"status": "active", "time": time.time()})

@app.route("/api/clans/delete", methods=["POST"])
def delete_clan():
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
        
    tag = request.args.get("tag", "").strip()
    
    keys_to_delete = [k for k in live_clan_stats if k.lower() == tag.lower()]
    for k in keys_to_delete:
        del live_clan_stats[k]
        if k in live_players_cache:
            del live_players_cache[k]
        
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM clans WHERE LOWER(clan_tag) = LOWER(%s)", (tag,))
                cur.execute("DELETE FROM clan_history WHERE LOWER(clan_tag) = LOWER(%s)", (tag,))
            conn.commit()
    return jsonify({"status": "deleted", "tag": tag})

@app.route("/api/history")
def get_history():
    clan_id = request.args.get("clan_tag", "").strip()
    period = request.args.get("period", "current")

    clan_tag = clan_id if clan_id.startswith("#") else f"#{clan_id}"
    this_month, prev_month = get_month_keys()

    target_month = this_month if period == "current" else prev_month

    rows = []
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Capped at LIMIT 72 to only show the last 3 days of hourly logs
                cur.execute("""
                    SELECT record_date, timestamp as time, latest_daily, hourly_rate
                    FROM clan_history
                    WHERE LOWER(clan_tag) = LOWER(%s) AND month_key = %s
                    ORDER BY id DESC LIMIT 72
                """, (clan_tag, target_month))
                rows = [dict(r) for r in cur.fetchall()]

    return jsonify({
        "period_label": target_month,
        "records": rows
    })

@app.route("/api/history_7day")
def get_7day_history():
    clan_id = request.args.get("clan_tag", "").strip()
    clan_tag = clan_id if clan_id.startswith("#") else f"#{clan_id}"
    
    rows = []
    if DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Get the final recorded value for each day exactly at reset time
                cur.execute("""
                    SELECT DISTINCT ON (record_date) 
                        record_date, 
                        latest_daily as max_daily
                    FROM clan_history
                    WHERE LOWER(clan_tag) = LOWER(%s)
                    ORDER BY record_date DESC, id DESC
                    LIMIT 7
                """, (clan_tag,))
                rows = [dict(r) for r in cur.fetchall()]
                
    return jsonify({"records": rows})

@app.route("/api/leaderboard")
def get_leaderboard():
    rows = []
    for tag, data in live_clan_stats.items():
        rows.append({
            "clan_tag": tag,
            "clan_name": data["name"],
            "season_donations": data["season_donations"],
            "latest_daily": data["latest_daily"]
        })
        
    rows.sort(key=lambda x: x["season_donations"], reverse=True)
    return jsonify({"leaderboard": rows})

@app.route("/api/top_players")
def get_top_players():
    # Gather players from all clans stored in memory
    all_players = []
    for clan_tag, players in live_players_cache.items():
        clan_name = live_clan_stats.get(clan_tag, {}).get("name", "Unknown Clan")
        for p in players:
            p_copy = p.copy()
            p_copy["clan_name"] = clan_name 
            all_players.append(p_copy)
            
    # Sort globally by expLevel from highest to lowest
    all_players.sort(key=lambda x: x.get("expLevel", 0), reverse=True)
    
    # Return the top 200
    return jsonify({"players": all_players[:200]})

@app.route("/api/track")
def track():
    clan_id = request.args.get("clan_tag", "").strip()
    if not clan_id and DATABASE_URL:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT clan_tag FROM clans WHERE is_active=1 LIMIT 1")
                row = cur.fetchone()
                if row:
                    clan_id = row[0]

    if not clan_id:
        return jsonify({"error": "No Clan Tag registered"}), 400

    payload, err = execute_clan_poll(clan_id)
    if err:
        return jsonify({"error": err}), 400

    return jsonify(payload)

@app.route("/api/player")
def player_details():
    player_tag = request.args.get("player_tag", "").strip()
    if not player_tag:
        return jsonify({"error": "No Player Tag provided"}), 400

    url = f"{BASE_URL}/players/{urllib.parse.quote(player_tag)}"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        p = resp.json()

        exp_level = p.get("expLevel", 1)
        return jsonify({
            "name": p.get("name", ""),
            "tag": p.get("tag", ""),
            "townHallLevel": p.get("townHallLevel", "N/A"),
            "role": p.get("role", "member").capitalize(),
            "warStars": p.get("warStars", 0),
            "expLevel": exp_level,
            "xpNeeded": get_xp_needed_for_next_level(exp_level),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

threading.Thread(target=background_worker_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    local_ip = get_local_ip()

    print("=" * 60)
    print("  COCTRACKER 2-Month Multi-Clan Telemetry Core Starting")
    print("=" * 60)
    print(f"  • Local / Network URL: http://{local_ip}:{port}")
    print(f"  • Active Port:         {port}")
    print("=" * 60)

    app.run(host="0.0.0.0", port=port, debug=False)
