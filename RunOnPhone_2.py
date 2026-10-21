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

app = Flask(__name__)

API_TOKEN = os.environ.get(
    "COC_API_TOKEN",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiIsImtpZCI6IjI4YTMxOGY3LTAwMDAtYTFlYi03ZmExLTJjNzQzM2M2Y2NhNSJ9.eyJpc3MiOiJzdXBlcmNlbGwiLCJhdWQiOiJzdXBlcmNlbGw6Z2FtZWFwaSIsImp0aSI6IjcxMzBkZjBhLTNlYWQtNGQyZi05NThkLTcxMzJkNWNjYTVkYyIsImlhdCI6MTc5MjUwNjkwOSwic3ViIjoiZGV2ZWxvcGVyL2JkMDQ1MjVkLTljMDItNGJmNy1iM2JhLTc1NTI3YWQwNDk5MSIsInNjb3BlcyI6WyJjbGFzaCJdLCJsaW1pdHMiOlt7InRpZXIiOiJkZXZlbG9wZXIvc2lsdmVyIiwidHlwZSI6InRocm90dGxpbmcifSx7ImNpZHJzIjpbIjQ1Ljc5LjIxOC43OSJdLCJ0eXBlIjoiY2xpZW50In1dfQ.E5EMziuzZqcG_maxsWk9CgoobLLSIbwxWedZh8ENy95b0Z1SsbS4Ofw1eY4H6XVtW89zoMEMe-wmyv7dm5yWrw"
)
BASE_URL = os.environ.get("COC_BASE_URL", "https://cocproxy.royaleapi.dev/v1")
DATA_STORE_FILE = os.path.join(os.path.dirname(__file__), "saved_clans.json")

# In-memory tracking state
clan_history = deque()
player_baselines = {}
player_hourly_history = {}  # {tag: deque([(timestamp, donated, received)])}
current_day = datetime.date.today()
current_tracked_clan = ""
cached_clan_payload = {}
tracking_lock = threading.Lock()


def load_persistent_store():
    if os.path.exists(DATA_STORE_FILE):
        try:
            with open(DATA_STORE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active_clan": "", "saved_clans": []}


def save_persistent_store(data):
    try:
        with open(DATA_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


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
  <title>COCTRACKER // 24/7 TELEMETRY</title>
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

    /* Saved Clans Bar */
    .saved-clans-bar {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding-bottom: 2px;
    }
    .clan-pill {
      background: #101726;
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 4px 10px;
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      white-space: nowrap;
    }
    .clan-pill.active {
      border-color: var(--neon-cyan);
      color: var(--neon-cyan);
      background: rgba(0, 224, 250, 0.08);
    }
    .clan-pill .del-btn {
      color: #ef4444;
      font-weight: 800;
      font-size: 10px;
      padding-left: 2px;
    }

    /* Input Card */
    .input-card {
      gap: 8px;
      padding: 10px;
    }
    input[type="text"] {
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
    input[type="text"]:focus {
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
    }
    .btn-apply:active {
      background-color: #005a8c;
    }

    /* Reset Timer Card */
    .reset-card {
      background-color: #0a121e;
      border: 1px solid rgba(0, 224, 250, 0.35);
      border-radius: 10px;
      padding: 10px 14px;
      gap: 4px;
    }
    .reset-header {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .icon-badge {
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      font-weight: 700;
      flex-shrink: 0;
    }
    .badge-amber {
      background: rgba(255, 184, 30, 0.15);
      color: var(--neon-amber);
      border: 1px solid rgba(255, 184, 30, 0.4);
    }
    .badge-cyan {
      background: rgba(0, 224, 250, 0.15);
      color: var(--neon-cyan);
      border: 1px solid rgba(0, 224, 250, 0.4);
    }
    .badge-emerald {
      background: rgba(16, 217, 133, 0.15);
      color: var(--neon-emerald);
      border: 1px solid rgba(16, 217, 133, 0.4);
    }
    .badge-purple {
      background: rgba(167, 115, 250, 0.15);
      color: var(--neon-purple);
      border: 1px solid rgba(167, 115, 250, 0.4);
    }
    .reset-title {
      font-size: 11px;
      font-weight: 700;
      color: var(--neon-amber);
      letter-spacing: 0.5px;
    }
    .reset-sub {
      font-size: 9px;
      color: var(--text-muted);
    }
    .timer-tag {
      font-size: 10px;
      font-weight: 700;
      color: var(--text-muted);
      text-align: center;
      margin-top: 4px;
      letter-spacing: 0.5px;
    }
    .timer-countdown {
      font-size: 28px;
      font-weight: 700;
      text-align: center;
      color: var(--text-white);
      letter-spacing: 1px;
    }

    /* 2x2 Metrics Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .metric-card {
      background: #0d131f;
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .metric-top {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .metric-title {
      font-size: 8.5px;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.5px;
    }
    .metric-val {
      font-size: 17px;
      font-weight: 700;
    }

    /* Section Header */
    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 4px 4px 0 4px;
    }
    .sec-title {
      font-size: 10.5px;
      font-weight: 700;
      color: var(--neon-cyan);
      letter-spacing: 0.5px;
    }
    .sec-sub {
      font-size: 10px;
      color: var(--text-muted);
    }

    /* Members List */
    .members-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .member-card {
      background-color: var(--card-inner-bg);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      cursor: pointer;
      transition: border-color 0.15s ease, transform 0.1s ease;
    }
    .member-card:hover {
      border-color: rgba(0, 224, 250, 0.5);
    }
    .member-card:active {
      transform: scale(0.99);
    }

    /* Member Top Bar */
    .member-top-bar {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .rank-box {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
      font-weight: 800;
      flex-shrink: 0;
    }
    .rank-1 { background-color: var(--neon-amber); color: #000; }
    .rank-2 { background-color: #cbd5e1; color: #000; }
    .rank-3 { background-color: #f97316; color: #000; }
    .rank-def { background-color: #212b3d; color: var(--text-white); }

    .exp-badge {
      padding: 3px 6px;
      background: rgba(16, 42, 85, 0.8);
      border: 1px solid rgba(56, 139, 253, 0.7);
      border-radius: 6px;
      font-size: 10.5px;
      font-weight: 700;
      color: #64b5f6;
      flex-shrink: 0;
    }
    .member-info-col {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      flex: 1;
    }
    .player-name {
      font-size: 13.5px;
      font-weight: 700;
      color: var(--text-white);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .player-tag {
      font-size: 9.5px;
      color: var(--text-muted);
    }
    .role-badge {
      padding: 3px 8px;
      background: rgba(46, 35, 15, 0.8);
      border: 1px solid rgba(255, 184, 30, 0.6);
      border-radius: 6px;
      font-size: 8.5px;
      font-weight: 700;
      color: var(--neon-amber);
      white-space: nowrap;
      flex-shrink: 0;
    }

    /* Member Stats Grid */
    .member-stats-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 6px;
      padding-top: 4px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }
    .stats-col {
      display: flex;
      flex-direction: column;
      gap: 1px;
    }
    .stats-col-title {
      font-size: 9px;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.5px;
      margin-bottom: 2px;
    }
    .stat-line {
      font-size: 9.5px;
      display: flex;
      gap: 3px;
      align-items: baseline;
    }
    .stat-lbl {
      font-size: 8px;
      color: var(--text-muted);
      font-weight: 600;
    }

    /* Footer */
    .footer-card {
      min-height: 30px;
      background-color: #06090e;
      border-color: #17202e;
      flex-direction: row;
      justify-content: space-between;
      align-items: center;
      padding: 4px 10px;
    }
    .footer-left {
      font-size: 10px;
      color: var(--text-muted);
    }
    .footer-right {
      font-size: 10px;
      font-weight: 700;
      color: var(--neon-cyan);
    }

    /* Modal */
    .modal-backdrop {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background-color: rgba(0, 0, 0, 0.75);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      z-index: 1000;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }
    .modal-backdrop.show {
      opacity: 1;
      pointer-events: auto;
    }
    .modal-card {
      width: 100%;
      max-width: 360px;
      background: #0f1624;
      border: 1px solid var(--neon-cyan);
      border-radius: 12px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      text-align: center;
      box-shadow: 0 10px 25px rgba(0,0,0,0.6);
    }
    .modal-title {
      font-size: 18px;
      font-weight: 700;
      color: var(--neon-cyan);
    }
    .modal-tag {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: -6px;
    }
    .modal-body {
      font-size: 13px;
      line-height: 1.6;
      color: var(--text-white);
      padding: 10px 0;
    }
    .btn-dismiss {
      height: 38px;
      background-color: #1c283c;
      color: var(--text-white);
      border: none;
      border-radius: 6px;
      font-weight: 700;
      font-size: 12px;
      cursor: pointer;
    }
    .btn-dismiss:active {
      background-color: #263650;
    }
  </style>
</head>
<body>
  <div class="app-window">
    <!-- Top Header -->
    <div class="card header-card">
      <div>
        <div class="clan-title" id="clanTitle">COCTRACKER // 24/7 TELEMETRY</div>
        <div class="sub-info" id="subInfo">Saved 24/7 Tracker Node Active</div>
      </div>
      <div class="badge-status" id="lblStatus">[ STANDBY ]</div>
    </div>

    <!-- Saved Clan Quick Selection -->
    <div class="saved-clans-bar" id="savedClansBar"></div>

    <!-- Add Clan Card -->
    <div class="card input-card" id="inputCard">
      <input type="text" id="inputClan" placeholder="Clan Tag (e.g. #2Y9...)" autocapitalize="characters" />
      <button class="btn-apply" id="btnApply">
        <span>+</span> Add clan
      </button>
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

    <!-- Members Section Header -->
    <div class="section-header">
      <span class="sec-title">CLAN MEMBERS | DONATION ROSTER</span>
      <span class="sec-sub" id="lblMemberSummary">0/50 Members</span>
    </div>

    <!-- Members Scroll Container -->
    <div class="members-list" id="membersList"></div>

    <!-- Telemetry Footer -->
    <div class="card footer-card">
      <span class="footer-left" id="lblFooterLeft">◷ Updated: Standby</span>
      <span class="footer-right" id="lblFooterRight">Next Poll: 60s</span>
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

  <script>
    const INTERVAL_SECONDS = 60;
    let clanId = "";
    let remainingTime = INTERVAL_SECONDS;

    const clanTitle = document.getElementById("clanTitle");
    const subInfo = document.getElementById("subInfo");
    const lblStatus = document.getElementById("lblStatus");
    const inputClan = document.getElementById("inputClan");
    const savedClansBar = document.getElementById("savedClansBar");
    const lblCountdown = document.getElementById("lblCountdown");
    const lblMemberSummary = document.getElementById("lblMemberSummary");
    const membersList = document.getElementById("membersList");

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

    btnDismissModal.onclick = () => playerModal.classList.remove("show");

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

    async function loadSavedClans() {
      try {
        const res = await fetch("/api/clans");
        const data = await res.json();
        renderSavedClansBar(data.saved_clans, data.active_clan);
        if (data.active_clan && !clanId) {
          clanId = data.active_clan;
          inputClan.value = clanId;
          fetchData();
        }
      } catch (e) {
        console.error("Failed loading saved clans", e);
      }
    }

    function renderSavedClansBar(clans, active) {
      savedClansBar.innerHTML = "";
      if (!clans || clans.length === 0) return;

      clans.forEach(c => {
        const pill = document.createElement("div");
        pill.className = `clan-pill ${c.tag === active ? "active" : ""}`;
        pill.innerHTML = `
          <span>${escapeHtml(c.name || c.tag)}</span>
          <span class="del-btn" title="Delete">✕</span>
        `;
        pill.querySelector("span:first-child").onclick = () => {
          clanId = c.tag;
          inputClan.value = c.tag;
          fetchData();
        };
        pill.querySelector(".del-btn").onclick = async (ev) => {
          ev.stopPropagation();
          await fetch(`/api/clans/delete?tag=${encodeURIComponent(c.tag)}`, { method: "POST" });
          loadSavedClans();
        };
        savedClansBar.appendChild(pill);
      });
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

        renderMembers(data.players);

        lblFooterLeft.textContent = `◷ Updated: ${data.timestamp}`;
        remainingTime = INTERVAL_SECONDS;
        lblFooterRight.textContent = `Next Poll: ${remainingTime}s`;
        loadSavedClans();
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

    document.getElementById("btnApply").addEventListener("click", () => {
      const clanVal = inputClan.value.trim();
      if (!clanVal) {
        lblStatus.textContent = "[ NO CLAN ]";
        lblStatus.style.color = "#ef4444";
        return;
      }
      clanId = clanVal;
      fetchData();
    });

    setInterval(() => {
      updateDailyResetCountdown();

      if (clanId) {
        remainingTime -= 1;
        if (remainingTime <= 0) {
          remainingTime = INTERVAL_SECONDS;
          fetchData();
        }
        lblFooterRight.textContent = `Next Poll: ${remainingTime}s`;
      }
    }, 1000);

    updateDailyResetCountdown();
    loadSavedClans();
  </script>
</body>
</html>
"""

def execute_clan_poll(clan_tag):
    global current_tracked_clan, current_day, clan_history, player_baselines, player_hourly_history, cached_clan_payload

    with tracking_lock:
        if not clan_tag:
            return None, "No Clan Tag provided"

        clan_tag = clan_tag if clan_tag.startswith("#") else f"#{clan_tag}"

        if clan_tag.lower() != current_tracked_clan.lower():
            clan_history.clear()
            player_baselines.clear()
            player_hourly_history.clear()
            current_tracked_clan = clan_tag

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

        now = datetime.datetime.now()
        today = now.date()
        current_epoch = now.timestamp()

        if today != current_day:
            current_day = today
            player_baselines.clear()

        clan_name = raw_clan_data.get("name", "Unknown Clan")
        member_count = raw_clan_data.get("members", len(raw_clan_data.get("memberList", [])))
        member_list = raw_clan_data.get("memberList", [])

        season_donations_total = 0
        season_received_total = 0
        latest_daily_total = 0
        parsed_members = []

        for member in member_list:
            donations = member.get("donations", 0)
            received = member.get("donationsReceived", 0)
            season_donations_total += donations
            season_received_total += received

            tag = member.get("tag", "").strip()
            name = member.get("name", "").strip()

            if tag not in player_baselines or donations < player_baselines[tag]:
                player_baselines[tag] = donations

            daily = donations - player_baselines[tag]
            latest_daily_total += daily

            # Hourly tracking per player
            if tag not in player_hourly_history:
                player_hourly_history[tag] = deque()
            p_history = player_hourly_history[tag]
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

        clan_history.append((current_epoch, season_donations_total))
        while clan_history and (current_epoch - clan_history[0][0]) > 3600:
            clan_history.popleft()

        clan_hourly = max(0, season_donations_total - clan_history[0][1])

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

        cached_clan_payload = payload

        # Keep persistent list updated with clan name
        store = load_persistent_store()
        store["active_clan"] = clan_tag
        found = False
        for c in store.get("saved_clans", []):
            if c["tag"].lower() == clan_tag.lower():
                c["name"] = clan_name
                found = True
                break
        if not found:
            store.setdefault("saved_clans", []).append({"tag": clan_tag, "name": clan_name})
        save_persistent_store(store)

        return payload, None


# --- 24/7 BACKGROUND MONITOR & PING WORKER ---
def background_telemetry_loop():
    while True:
        try:
            store = load_persistent_store()
            active_clan = store.get("active_clan")
            if active_clan:
                execute_clan_poll(active_clan)

            # Keep-alive self-ping for free hosting (Render / PaaS)
            self_url = os.environ.get("RENDER_EXTERNAL_URL")
            if self_url:
                requests.get(f"{self_url}/api/ping", credentials=None, timeout=5)
        except Exception as e:
            pass
        time.sleep(60)


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/ping")
def ping():
    return jsonify({"status": "healthy", "timestamp": time.time()})


@app.route("/api/clans")
def get_clans():
    store = load_persistent_store()
    return jsonify(store)


@app.route("/api/clans/delete", methods=["POST"])
def delete_clan():
    tag = request.args.get("tag", "").strip()
    store = load_persistent_store()
    store["saved_clans"] = [c for c in store.get("saved_clans", []) if c["tag"].lower() != tag.lower()]
    if store.get("active_clan", "").lower() == tag.lower():
        store["active_clan"] = store["saved_clans"][0]["tag"] if store["saved_clans"] else ""
    save_persistent_store(store)
    return jsonify(store)


@app.route("/api/track")
def track():
    clan_id = request.args.get("clan_tag", "").strip()
    if not clan_id:
        store = load_persistent_store()
        clan_id = store.get("active_clan", "")

    if not clan_id:
        return jsonify({"error": "No Clan Tag provided"}), 400

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


# Start 24/7 background worker thread
threading.Thread(target=background_telemetry_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    local_ip = get_local_ip()

    print("=" * 60)
    print("  COCTRACKER 24/7 Telemetry Server Starting")
    print("=" * 60)
    print(f"  • Local / Network URL: http://{local_ip}:{port}")
    print(f"  • Active Port:         {port}")
    print("=" * 60)

    app.run(host="0.0.0.0", port=port, debug=False)
